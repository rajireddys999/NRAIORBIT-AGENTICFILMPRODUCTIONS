"""5-minute POC project, bill estimate, shot pack export and inbox import."""

import json

import pytest

from nraiorbit.config import REPO_ROOT, deep_merge, load_navarasa, load_pipeline_config
from nraiorbit.costing import billed_seconds, estimate, load_pricing, render_markdown
from nraiorbit.library.registry import AssetRegistry
from nraiorbit.pipeline import Pipeline
from nraiorbit.planning.runtime import validate_outline
from nraiorbit.poc import create_poc
from nraiorbit.post import ffmpeg
from nraiorbit.providers.indic_parler import describe
from nraiorbit.schemas import Outline, SceneScript, ScriptBlock, ShotList
from nraiorbit.shotpack import clips_for, export, status


@pytest.fixture
def poc(tmp_path):
    return create_poc(tmp_path / "godavari5", automatic=False)


@pytest.mark.parametrize("duration, expected", [
    (3, (4, 1)), (4, (4, 1)), (4.2, (6, 1)), (8, (8, 1)), (9, (15, 2)), (16, (22, 3)),
])
def test_veo_billing_rounds_up_to_clip_lengths(duration, expected):
    assert billed_seconds(duration, [4, 6, 8], 7) == expected


def test_poc_plan_is_consistent(poc):
    outline = Outline.model_validate_json((poc / "02_outline.json").read_text())
    assert sum(s.target_seconds for s in outline.scenes) == 300
    assert validate_outline(outline, 5, 0.5) == []
    total_shots = 0
    for sc in outline.scenes:
        shots = ShotList.model_validate_json((poc / "04_shots" / f"{sc.scene_id}.json").read_text()).shots
        script = SceneScript.model_validate_json((poc / "03_scenes" / f"{sc.scene_id}.json").read_text())
        total_shots += len(shots)
        assert sum(s.duration_s for s in shots) == pytest.approx(sc.target_seconds)
        assert max(s.duration_s for s in shots) <= 8, "Veo generates at most 8 s per clip"
        covered = {b for s in shots for b in s.block_ids}
        assert {b.block_id for b in script.blocks} <= covered
        for b in script.blocks:
            if b.kind == "dialogue":
                assert b.character_id in {"arjun", "meera", "bhairava"} and b.text_te.strip()
    assert total_shots == 54
    cues = json.loads((poc / "08_music" / "cue_sheet.json").read_text())["cues"]
    assert {c["leitmotif_character"] for c in cues} <= {"arjun", None}   # only arjun has a motif in the library
    with pytest.raises(FileExistsError):
        create_poc(poc)


def test_estimate_for_poc(poc):
    pricing = load_pricing(poc)
    e = estimate(poc, pricing=pricing)
    assert len(e.stats.shots) == 54 and e.stats.lines == 13
    assert e.veo_clips == 54
    assert e.video_options["fast"]["expected"] == pytest.approx(e.veo_seconds * 2.0 * 0.10)
    assert e.claude_billing == "subscription"
    # manual Veo: no pipeline keyframes or Gemini TTS; one-time reference images are still counted
    assert e.keyframes["expected"] == 0 and e.tts["expected"] == 0
    assert e.other_costs("expected") == pytest.approx(e.lipsync["expected"] + e.storage["expected"] + e.identity_pack)
    report = render_markdown(e, pricing)
    assert "Veo 3.1 Fast" in report and "Claude Code" in report


def test_estimate_automatic_mode_counts_keyframes_and_tts(tmp_path):
    root = create_poc(tmp_path / "auto")
    e = estimate(root)
    assert e.veo_mode == "keyframe" and e.keyframes["expected"] > 0 and e.tts["expected"] > 0


def test_estimate_from_runtime_only():
    e = estimate(None, runtime_minutes=105)
    assert len(e.stats.shots) == 1400 and not e.stats.from_plan
    assert e.claude_api["expected"] > 0


def _cast(poc):
    cfg = deep_merge(load_pipeline_config(poc), {"animatic": {"resolution": [320, 180]},
                                                 "film": {"resolution": [320, 180]}})
    pipeline = Pipeline(poc, llm=None, cfg=cfg, log=lambda m: None)
    pipeline.run(until="cast")      # library-first casting: no LLM call
    return pipeline


def test_shotpack_export(poc):
    _cast(poc)
    pack = export(poc, AssetRegistry.load(REPO_ROOT / "library"), load_navarasa(), load_pricing(poc))
    prompts = sorted(pack.glob("*/prompt.txt"))
    assert len(prompts) == 54
    text = (pack / "sc002_s005" / "prompt.txt").read_text()
    assert "Arjun" in text and "veshti" in text and "Clip length: 8 s" in text
    assert "inbox/video/sc002_s005.mp4" in text
    assert "Reference images missing" in (pack / "SHOTPACK.md").read_text()   # example library has no media
    assert "sc002_b003" in (pack / "DIALOGUE.md").read_text()
    assert "cue002" in (pack / "MUSIC.md").read_text()


def test_clip_attempt_ordering(poc):
    inbox = poc / "inbox" / "video"
    inbox.mkdir(parents=True)
    for name in ("sc001_s001_3.mp4", "sc001_s001.mp4", "sc001_s001_2.mp4", "sc001_s0010.mp4", "notes.txt"):
        (inbox / name).write_bytes(b"")
    assert [p.name for p in clips_for(poc, "sc001_s001")] == ["sc001_s001.mp4", "sc001_s001_2.mp4", "sc001_s001_3.mp4"]


def test_telugu_tts_description():
    line = ScriptBlock.model_validate({
        "block_id": "x", "kind": "dialogue", "character_id": "arjun", "text_te": "భయపడకు.", "text_en": "Don't be afraid.",
        "delivery": {"rasa": "karuna", "intensity": 6, "pace": "slow", "volume": "low", "notes": ""}})
    d = describe({"tts_speaker": "Prakash", "pitch": "low", "timbre_tags": ["warm"]}, line)
    assert d.startswith("Prakash") and "slowly" in d and "sad" in d and "deep" in d


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
def test_inbox_import_assembles_real_clips(poc):
    pipeline = _cast(poc)
    inbox = poc / "inbox"
    for kind in ("video", "dialogue", "music"):
        (inbox / kind).mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(inbox / "video" / "sc002_s005.mp4")])
    ffmpeg.run(["-f", "lavfi", "-i", "sine=f=500:d=6.5", "-ac", "1", "-ar", "44100", str(inbox / "dialogue" / "sc002_b003.wav")])
    ffmpeg.run(["-f", "lavfi", "-i", "sine=f=110:d=90", str(inbox / "music" / "cue002.mp3")])

    pipeline.run(until="render")

    take = poc / "07_video" / "sc002_s005" / "take_1.mp4"
    assert ffmpeg.duration(take) == pytest.approx(7.0, abs=0.05)          # trimmed to the shot, at 24 fps
    timeline = json.loads((poc / "10_edit" / "timeline.json").read_text())
    line = next(e for e in timeline["dialogue"] if e["ref_id"] == "sc002_b003")
    assert line["duration_s"] == pytest.approx(6.5, abs=0.05)              # the recording, not the fallback
    assert ffmpeg.duration(poc / "08_music" / "cue002.wav") == pytest.approx(38.5, abs=0.1)  # 40 s cue - 1.5 s silence
    assert status(poc)["attempts"] == 1
    actual = estimate(poc).actual_veo
    assert actual["clips"] == 1 and actual["billed_seconds"] == 8
