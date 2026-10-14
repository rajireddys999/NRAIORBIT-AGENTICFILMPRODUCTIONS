"""Per-shot Veo tiers, Vertex video-only prices, verified Google prices, lip-sync timeout and the upscale filter."""

import json

import pytest

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.costing import (UnknownVideoTier, estimate, load_pricing, preview_run, render_markdown,
                               veo_clip_seconds, veo_price_per_second, veo_settings)
from nraiorbit.poc import create_poc
from nraiorbit.post import ffmpeg
from nraiorbit.providers.cloud_lipsync import gpu_price
from nraiorbit.schemas import Shot, ShotList


def set_tiers(root, tiers: dict[str, str | None]) -> list[Shot]:
    """Give shots a video_tier in their shot list; returns the changed shots."""
    changed = []
    for path in sorted((root / "04_shots").glob("*.json")):
        shot_list = ShotList.model_validate_json(path.read_text())
        for shot in shot_list.shots:
            if shot.shot_id in tiers:
                shot.video_tier = tiers[shot.shot_id]
                changed.append(shot)
        path.write_text(shot_list.model_dump_json(indent=2))
    return changed


def test_shot_without_video_tier_still_loads(tmp_path):
    root = create_poc(tmp_path / "poc")
    raw = json.loads((root / "04_shots" / "sc002.json").read_text())
    for s in raw["shots"]:
        s.pop("video_tier", None)
    shots = ShotList.model_validate(raw).shots
    assert all(s.video_tier is None for s in shots)


def test_director_is_not_asked_for_a_video_tier():
    schema = Shot.model_json_schema()                # tiers are a spending choice, assigned by hand
    assert "video_tier" not in schema["properties"]
    assert set(schema["required"]) == set(schema["properties"])
    assert Shot.model_validate({**_any_shot(), "video_tier": "fast_720p"}).video_tier == "fast_720p"


def _any_shot() -> dict:
    return {"shot_id": "sc001_s001", "block_ids": [], "size": "WS", "camera_move": "static", "lens_mm": 35,
            "fps_effect": "normal", "characters_in_frame": [], "costume_ids": [], "action_template_id": None,
            "image_prompt": "", "motion_prompt": "", "duration_s": 4.0, "rasa": "shanta", "intensity": 1,
            "sfx_cues": [], "trailer_candidate": False, "punch_dialogue": False}


def test_default_video_tiers_and_lipsync_timeout():
    cfg = load_pipeline_config()
    tiers = cfg["google"]["video_tiers"]
    assert tiers["standard_1080p"] == {"model": "veo-3.1-generate-001", "tier": "standard", "resolution": "1080p"}
    assert tiers["standard_720p"] == {"model": "veo-3.1-generate-001", "tier": "standard", "resolution": "720p"}
    assert tiers["fast_720p"] == {"model": "veo-3.1-fast-generate-001", "tier": "fast", "resolution": "720p"}
    assert tiers["lite_720p"] == {"model": "veo-3.1-lite-generate-001", "tier": "lite", "resolution": "720p"}
    assert cfg["lipsync"]["timeout_seconds"] == 21600


def test_veo_settings_per_shot(tmp_path):
    g = load_pipeline_config()["google"] | {"veo_model": "m-default", "veo_tier": "standard", "veo_resolution": "1080p"}
    shot = ShotList.model_validate_json((create_poc(tmp_path / "poc") / "04_shots" / "sc002.json").read_text()).shots[0]
    assert veo_settings(g, shot) == ("m-default", "standard", "1080p")
    assert veo_settings(g, shot.model_copy(update={"video_tier": "lite_720p"})) == \
        ("veo-3.1-lite-generate-001", "lite", "720p")
    with pytest.raises(UnknownVideoTier, match="video_tier 'turbo_4k' is not in google.video_tiers.*fast_720p"):
        veo_settings(g, shot.model_copy(update={"video_tier": "turbo_4k"}))


@pytest.mark.parametrize("vertex, audio, expected", [
    (True, False, {"standard": (0.20, 0.20), "fast": (0.08, 0.10), "lite": (0.03, 0.05)}),   # video only
    (True, True, {"standard": (0.40, 0.40), "fast": (0.10, 0.12), "lite": (0.05, 0.08)}),
    (False, False, {"standard": (0.40, 0.40), "fast": (0.10, 0.12), "lite": (0.05, 0.08)}),  # Gemini API
])
def test_veo_price_per_second(vertex, audio, expected):
    pricing = load_pricing()
    g = {"vertex": vertex, "veo_audio": audio}
    for tier, (p720, p1080) in expected.items():
        assert veo_price_per_second(pricing, g, tier, "720p") == pytest.approx(p720)
        assert veo_price_per_second(pricing, g, tier, "1080p") == pytest.approx(p1080)


def test_verified_google_prices():
    pricing = load_pricing()
    assert pricing["lyria"]["per_generation"]["lyria-3-pro-preview"] == pytest.approx(0.08)
    assert gpu_price(pricing, "vertex_endpoint") == {"per_hour": pytest.approx(0.98181), "startup_minutes": 25,
                                                     "minutes_per_clip": 10.0}


def _veo_cfg(root, **google):
    return deep_merge(load_pipeline_config(root), {"google": google, "render": {"only": []}})


def test_preview_prices_each_shot_with_its_own_tier(tmp_path):
    root = create_poc(tmp_path / "poc")              # Veo 3.1 standard, 1080p by default
    pricing = load_pricing(root)
    base = preview_run(root, _veo_cfg(root, vertex=True), pricing)
    assert base["video_usd"] == pytest.approx(base["seconds"] * 0.20)          # Vertex, no audio: video-only rate
    assert base["seconds"] == 54 * 8                                            # 1080p: 8 s clips only

    fast, lite = set_tiers(root, {"sc002_s005": "fast_720p", "sc003_s001": "lite_720p"})
    tiered = preview_run(root, _veo_cfg(root, vertex=True), pricing)
    fast_s = veo_clip_seconds(fast.duration_s, pricing, "720p", "keyframe", bool(fast.characters_in_frame))
    lite_s = veo_clip_seconds(lite.duration_s, pricing, "720p", "keyframe", bool(lite.characters_in_frame))
    assert fast_s < 8 or lite_s < 8                                             # 720p allows 4/6 s clips
    assert tiered["seconds"] == 52 * 8 + fast_s + lite_s
    assert tiered["video_usd"] == pytest.approx(52 * 8 * 0.20 + fast_s * 0.08 + lite_s * 0.03)

    gemini_api = preview_run(root, _veo_cfg(root, vertex=False), pricing)       # with audio: api_per_second
    assert gemini_api["video_usd"] == pytest.approx(52 * 8 * 0.40 + fast_s * 0.10 + lite_s * 0.05)

    set_tiers(root, {"sc002_s005": "bogus"})
    with pytest.raises(UnknownVideoTier, match="sc002_s005: video_tier 'bogus'"):
        preview_run(root, _veo_cfg(root, vertex=True), pricing)


def test_estimate_uses_the_projects_billing(tmp_path):
    root = create_poc(tmp_path / "poc")
    pricing = load_pricing(root)
    vertex = estimate(root, pricing=pricing, cfg=_veo_cfg(root, vertex=True))
    api = estimate(root, pricing=pricing, cfg=_veo_cfg(root, vertex=False))
    assert vertex.video_rates == {"standard": 0.20, "fast": 0.10, "lite": 0.05}        # 1080p, video only
    assert vertex.video_options["standard"]["expected"] == pytest.approx(api.video_options["standard"]["expected"] / 2)
    assert "API: Veo 3.1 ($0.2/s)" in render_markdown(vertex, pricing)
    assert "API: Veo 3.1 ($0.4/s)" in render_markdown(api, pricing)
    assert vertex.video_planned == {} and "as planned" not in render_markdown(vertex, pricing)

    fast, lite = set_tiers(root, {"sc002_s005": "fast_720p", "sc003_s001": "lite_720p"})
    planned = estimate(root, pricing=pricing, cfg=_veo_cfg(root, vertex=True))
    fast_s = veo_clip_seconds(fast.duration_s, pricing, "720p", "keyframe", False)
    lite_s = veo_clip_seconds(lite.duration_s, pricing, "720p", "keyframe", False)
    one_attempt = 52 * 8 * 0.20 + fast_s * 0.08 + lite_s * 0.03
    att = pricing["attempts_per_clip"]["expected"]
    assert planned.video_planned["expected"] == pytest.approx(one_attempt * att)
    assert "| API: as planned, each shot at its video_tier (2 of 54 shots set) |" in render_markdown(planned, pricing)


def test_shotpack_names_the_shots_tier(tmp_path):
    import shutil

    from nraiorbit.config import REPO_ROOT, load_navarasa
    from nraiorbit.library.registry import AssetRegistry
    from nraiorbit.pipeline import Pipeline
    from nraiorbit.shotpack import export

    root = create_poc(tmp_path / "poc", automatic=False)
    Pipeline(root, llm=None, cfg=load_pipeline_config(root), log=lambda m: None).run(until="cast")
    lib_root = tmp_path / "library"
    shutil.copytree(REPO_ROOT / "library", lib_root, ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.jpeg"))
    (shot,) = set_tiers(root, {"sc002_s005": "fast_720p"})
    pack = export(root, AssetRegistry.load(lib_root), load_navarasa(), load_pricing(root),
                  load_pipeline_config(root)["google"])
    md = (pack / "SHOTPACK.md").read_text()
    clip_s = veo_clip_seconds(shot.duration_s, load_pricing(root), "720p", "keyframe", False)
    assert f"### sc002_s005 · {clip_s} s · Veo 3.1 Fast 720p · " in md
    assert "Model: Veo 3.1 Fast 720p" in (pack / "sc002_s005" / "prompt.txt").read_text()
    assert "Model:" not in (pack / "sc002_s001" / "prompt.txt").read_text()
    assert "### sc002_s001 · 8 s · " in md                    # untiered: the project's 1080p, 8 s clips only


def test_conform_scales_with_lanczos(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ffmpeg, "run", lambda args, **kw: seen.append(args))
    ffmpeg.conform_clip(tmp_path / "in.mp4", tmp_path / "out.mp4", 6.0, (1920, 1080), 24)
    vf = seen[0][seen[0].index("-vf") + 1]
    assert vf.startswith("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=")
