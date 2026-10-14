"""End-to-end: recorded agents + mock providers -> animatic movie, trailer, subtitles."""

import copy
import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
import yaml

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
from nraiorbit.pipeline import DIALOGUE_GAP_S, GatePending, Pipeline
from nraiorbit.post import ffmpeg
from nraiorbit.post.speech import tighten_pauses
from nraiorbit.post.trailer import PUNCH_DROP_S

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
SR = ffmpeg.SAMPLE_RATE


def _samples(path: Path) -> np.ndarray:
    """Any audio (or a video's sound) as mono float samples at 48 kHz."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def _db(x: np.ndarray, a: float, b: float) -> float:
    seg = x[int(a * SR):int(b * SR)]
    return float(20 * np.log10(np.sqrt((seg.astype(np.float64) ** 2).mean()) + 1e-9))


def _write_wav(path: Path, x: np.ndarray) -> Path:
    stereo = np.repeat((np.clip(x, -1, 1) * 32767).astype("<i2")[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())
    return path


def _assert_no_overlap(dialogue: list[dict]) -> None:
    spans = sorted((e["start_s"], e["start_s"] + e["duration_s"]) for e in dialogue)
    assert all(b[0] - a[1] >= DIALOGUE_GAP_S - 1e-3 for a, b in zip(spans, spans[1:])), spans


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
    _assert_no_overlap(timeline["dialogue"])

    # trailer: picture cut without the film's sound, whole lines laid over it, none chopped
    plan = json.loads((root / "10_edit" / "trailer_plan.json").read_text())
    carried = [c for c in plan if c["line_id"]]
    assert {c["section"] for c in carried} >= {"punch", "conflict"}
    trailer_dialogue = _samples(root / "10_edit" / ".trailer_dialogue" / "dialogue.wav")
    paths = {e["ref_id"]: e["path"] for e in timeline["dialogue"]}
    for c in carried:
        assert c["line_at_s"] + c["line_duration_s"] <= c["trailer_start_s"] + c["duration_s"] + 1e-6
        line = _samples(Path(paths[c["line_id"]]))
        at = int(round(c["line_at_s"] * SR))
        assert at + len(line) <= len(trailer_dialogue)
        assert np.abs(trailer_dialogue[at:at + len(line)] - line).max() < 1e-3      # the whole line, uncut
    cue = json.loads((root / "08_music" / "trailer_cue.json").read_text())
    assert re.match(r"[^;]*0\.0-\d+\.\ds world", cue["notes"])
    assert re.search(r"\d+\.\ds: 0\.7 s near-silence drop before the punch line", cue["notes"])
    assert "huge hit on the title card" in cue["notes"]
    # the score dips about 20 dB right before the punch line
    mix = _samples(master / "trailer.mp4")
    drop = next(c for c in carried if c["section"] == "punch")["line_at_s"] - PUNCH_DROP_S
    assert _db(mix, drop + 0.1, drop + PUNCH_DROP_S - 0.1) < _db(mix, drop - 0.8, drop - 0.1) - 12

    # re-running is a no-op: every stage is cached, no new LLM calls
    calls = len(llm.calls)
    Pipeline(root, llm, cfg=cfg, log=lambda m: None).run()
    assert len(llm.calls) == calls


def test_long_line_runs_into_the_next_scene_intact(tmp_path):
    """A line longer than what is left of its scene is neither pulled earlier nor cut at the scene cut."""
    from nraiorbit.providers.base import register
    from nraiorbit.providers.mock import MockTTS

    long_id, long_s = "sc001_b002", 22.5             # starts 14.2 s into a 25 s scene and ends 11.7 s into the next

    @register("tts", "long_line_test")
    class LongLineTTS(MockTTS):
        def synthesize(self, line, voice_ref, out):
            if line.block_id != long_id:
                return super().synthesize(line, voice_ref, out)
            out.parent.mkdir(parents=True, exist_ok=True)
            ffmpeg.run(["-f", "lavfi", "-i", f"sine=frequency=330:duration={long_s}:sample_rate={SR}",
                        "-af", "volume=0.4", "-ac", "2", str(out)])
            return long_s

    root, cfg = _project(tmp_path)
    cfg["providers"]["tts"] = "long_line_test"
    Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None).run(until="assemble")

    timeline = json.loads((root / "10_edit" / "timeline.json").read_text())
    _assert_no_overlap(timeline["dialogue"])
    lines = {e["ref_id"]: e for e in timeline["dialogue"]}
    line, scene_end = lines[long_id], json.loads((root / "02_outline.json").read_text())["scenes"][0]["target_seconds"]
    assert line["start_s"] == pytest.approx(14.2) and line["duration_s"] == pytest.approx(long_s)
    assert line["start_s"] + long_s > scene_end + 10
    assert lines["sc002_b002"]["start_s"] >= line["start_s"] + long_s + DIALOGUE_GAP_S - 1e-3   # the next line waits

    stem = _samples(root / "11_master" / "stems" / "dialogue.wav")
    source = _samples(Path(line["path"]))
    at = int(round(line["start_s"] * SR))
    assert len(source) == pytest.approx(long_s * SR, abs=2)
    assert np.abs(stem[at:at + len(source)] - source).max() < 1e-3           # sample-exact across the scene cut
    level = _db(source, 0, long_s)
    assert _db(stem, scene_end, scene_end + 10) == pytest.approx(level, abs=0.5)       # still speaking after the cut
    assert _db(stem, scene_end - 0.05, scene_end + 0.05) == pytest.approx(level, abs=0.5)  # no gap at the cut
    srt = (root / "11_master" / "subs_te.srt").read_text(encoding="utf-8")
    assert "00:00:14,200 --> 00:00:36,700" in srt                            # subtitles keep the real film time


def test_place_audio_trims_heads_and_tails_in_whole_samples(tmp_path):
    t = np.arange(2 * SR) / SR
    source = _write_wav(tmp_path / "line.wav", np.sin(2 * np.pi * 440 * t) * np.where(t < 1.0, 0.2, 0.6))
    expect = _samples(source)
    head = _samples(ffmpeg.place_audio([(source, -1.0, 0.0)], 1.5, tmp_path / "head.wav"))
    assert len(head) == int(1.5 * SR)                                        # exactly total_s
    assert np.abs(head[:SR] - expect[SR:]).max() < 1e-3                      # the head is gone, the rest starts at 0
    assert np.abs(head[SR:]).max() < 1e-4
    tail = _samples(ffmpeg.place_audio([(source, 1.2, 0.0)], 1.5, tmp_path / "tail.wav"))
    at = int(1.2 * SR)
    assert len(tail) == int(1.5 * SR) and np.abs(tail[:at]).max() < 1e-4
    assert np.abs(tail[at:] - expect[:len(tail) - at]).max() < 1e-3          # cut at the end
    # a line split at a scene cut (0.8 s in the first scene, the rest in the next) joins back sample-exact
    first = _samples(ffmpeg.place_audio([(source, 0.7, 0.0)], 1.5, tmp_path / "a.wav"))
    second = _samples(ffmpeg.place_audio([(source, 0.7 - 1.5, 0.0)], 2.0, tmp_path / "b.wav"))
    joined = np.concatenate([first, second])
    start = int(0.7 * SR)
    assert np.abs(joined[start:start + 2 * SR] - expect).max() < 1e-3


def test_tighten_pauses_trims_edges_and_long_gaps(tmp_path):
    def tone(s):
        return 0.3 * np.sin(2 * np.pi * 220 * np.arange(int(s * SR)) / SR)

    def gap(s):
        return np.zeros(int(s * SR))

    x = np.concatenate([gap(0.5), tone(1.0), gap(1.1), tone(0.8), gap(0.4), tone(0.5), gap(0.9)])
    take = _write_wav(tmp_path / "take.wav", x)
    words = (np.abs(_samples(take)) > 0.05).sum()
    new = tighten_pauses(take, max_pause_s=0.6, keep_s=0.3)
    # edges down to 0.06 s, the 1.1 s pause down to 0.3 s, the 0.4 s pause kept
    assert new == pytest.approx(0.06 + 1.0 + 0.3 + 0.8 + 0.4 + 0.5 + 0.06, abs=0.03)
    y = _samples(take)
    assert len(y) / SR == pytest.approx(new, abs=1e-3)
    assert (np.abs(y) > 0.05).sum() == words                                 # every word kept, sample for sample
    steady = _write_wav(tmp_path / "steady.wav", tone(1.0))
    assert tighten_pauses(steady) == pytest.approx(1.0, abs=1e-3)


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


def test_title_card_is_cinematic_not_text_on_black(tmp_path):
    from nraiorbit.post import assemble

    src = tmp_path / "film.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)])
    out = assemble.cut_segments(src, [(None, 2.0)], tmp_path / "t.mp4", title="Godavari Simham",
                                resolution=(320, 180), fps=24, audio=False, title_backdrop_s=1.0)
    assert ffmpeg.duration(out) == pytest.approx(2.0, abs=0.05)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", str(out), "-frames:v", "1",
                                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(180, 320, 3).astype(float)
    assert frame.mean() > 8                                   # a blurred film frame behind it, not black
    edge = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                                         "-pix_fmt", "gray", "-"], capture_output=True).stdout
    assert np.frombuffer(edge, np.uint8).mean() < frame.mean()  # faded in from dark
    designed = tmp_path / "title.png"
    ffmpeg.run(["-f", "lavfi", "-i", "color=c=0xAA6622:s=640x360", "-frames:v", "1", str(designed)])
    with_image = assemble.cut_segments(src, [(None, 2.0)], tmp_path / "ti.mp4", resolution=(320, 180), fps=24,
                                       audio=False, title_image=designed)
    assert ffmpeg.duration(with_image) == pytest.approx(2.0, abs=0.05)
