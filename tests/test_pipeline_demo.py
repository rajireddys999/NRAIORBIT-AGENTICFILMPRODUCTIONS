"""End-to-end: recorded agents + mock providers -> animatic movie, trailer, subtitles."""

import copy
import json

import pytest
import yaml

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
from nraiorbit.pipeline import GatePending, Pipeline
from nraiorbit.post import ffmpeg

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")


def _project(tmp_path, gates_auto=True):
    root = tmp_path / "godavari"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = copy.deepcopy(DEMO_CONFIG)
    if not gates_auto:
        cfg["review_gates"]["story"] = {"enabled": True, "auto_approve": False}
    return root, deep_merge(load_pipeline_config(), cfg)


def test_demo_end_to_end(tmp_path):
    root, cfg = _project(tmp_path)
    llm = demo_llm()
    Pipeline(root, llm, cfg=cfg, log=lambda m: None).run()

    master = root / "11_master"
    movie = ffmpeg.duration(master / "movie.mp4")
    assert movie == pytest.approx(180, abs=1.0)
    assert ffmpeg.duration(master / "trailer.mp4") <= 31
    srt = (master / "subs_te.srt").read_text(encoding="utf-8")
    assert "నా మాటే శాసనం" in srt

    lock = yaml.safe_load((root / "cast.lock.yaml").read_text(encoding="utf-8"))
    assert lock["characters"]["arjun"]["character_asset"] == "characters/arjun_hero@v3"
    assert lock["characters"]["arjun"]["leitmotif_asset"] == "music/motifs/arjun_theme@v1"

    timeline = json.loads((root / "10_edit" / "timeline.json").read_text())
    assert len(timeline["dialogue"]) == 7
    # every scene's shots fill its budget exactly
    by_scene = {}
    for s in timeline["shots"]:
        by_scene[s["scene_id"]] = by_scene.get(s["scene_id"], 0) + s["duration_s"]
    for sc in json.loads((root / "02_outline.json").read_text())["scenes"]:
        assert by_scene[sc["scene_id"]] == pytest.approx(sc["target_seconds"], abs=1e-6)

    # re-running is a no-op: every stage is cached, no new LLM calls
    calls = len(llm.calls)
    Pipeline(root, llm, cfg=cfg, log=lambda m: None).run()
    assert len(llm.calls) == calls


def test_gate_pauses_and_force_invalidates(tmp_path):
    root, cfg = _project(tmp_path, gates_auto=False)
    pipeline = Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None)
    with pytest.raises(GatePending) as info:
        pipeline.run()
    assert info.value.gate == "story"
    assert (root / "reviews" / "story.pending").exists()
    assert not (root / "02_outline.json").exists()

    (root / "reviews" / "story.approved").write_text("ok")
    pipeline.run(until="outline")
    assert (root / "02_outline.json").exists()

    pipeline.invalidate("outline")
    assert (root / "01_story_bible.json").exists()
    assert not (root / "02_outline.json").exists()


def test_identity_guard_drives_retakes(tmp_path):
    """Take 1 of every shot shows strangers, take 2 the locked faces: the guard must pick take 2."""
    import shutil

    import numpy as np

    from nraiorbit.config import REPO_ROOT
    from nraiorbit.guards.identity import FaceObservation
    from nraiorbit.library.registry import AssetRegistry
    from nraiorbit.providers.base import register

    lib_root = tmp_path / "library"
    shutil.copytree(REPO_ROOT / "library", lib_root)
    rng = np.random.default_rng(3)
    centroids = {}
    for char_dir in ("arjun_hero", "meera_heroine", "bhairava_villain"):
        v = rng.normal(size=512)
        centroids[char_dir] = v / np.linalg.norm(v)
        (lib_root / "characters" / char_dir / "embeddings").mkdir(parents=True, exist_ok=True)
        np.save(lib_root / "characters" / char_dir / "embeddings" / "face_centroid.npy", centroids[char_dir])

    @register("face_embedder", "fake_test")
    class FakeEmbedder:
        def detect_and_embed(self, image_path):
            if "take_1" in str(image_path):
                return [FaceObservation(rng.normal(size=512), (0, 0, 200, 200))]
            return [FaceObservation(c, (0, 0, 200, 200)) for c in centroids.values()]

    root, cfg = _project(tmp_path)
    cfg["providers"]["face_embedder"] = "fake_test"
    cfg["retakes"]["max_per_shot"] = 2
    Pipeline(root, demo_llm(), cfg=cfg, library=AssetRegistry.load(lib_root), log=lambda m: None).run(until="render")

    timeline = json.loads((root / "10_edit" / "timeline.json").read_text())
    shots_with_people = [s for s in timeline["shots"] if (root / "07_video" / s["shot_id"] / "take_2.mp4").exists()]
    assert shots_with_people, "guard never asked for a retake"
    assert all(s["clip"].endswith("take_2.mp4") for s in shots_with_people)
    decisions = [json.loads(line) for line in (root / "reports" / "guards.jsonl").read_text().splitlines()]
    assert {d["take"] for d in decisions} == {1, 2}
    assert all(d["passed"] == (d["take"] == 2) for d in decisions)
    assert not (root / "reports" / "review_queue.jsonl").exists()
