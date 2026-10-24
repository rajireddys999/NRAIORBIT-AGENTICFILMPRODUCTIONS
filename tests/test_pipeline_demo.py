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
from nraiorbit.pipeline import DIALOGUE_GAP_S, GatePending, LinesTooLong, Pipeline
from nraiorbit.post.cards import END_CARD_S
from nraiorbit.post import ffmpeg
from nraiorbit.post.speech import level_gain_db, tighten_pauses
from nraiorbit.post.trailer import PUNCH_DROP_S

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
SR = ffmpeg.SAMPLE_RATE


def _samples(path: Path) -> np.ndarray:
    """Any audio (or a video's sound) as mono float samples at 48 kHz."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def _levelled(path: Path, cfg: dict) -> np.ndarray:
    """A take's samples at the speaking level the mix brings every line to (audio.dialogue_level_dbfs)."""
    target = cfg["audio"].get("dialogue_level_dbfs")
    return _samples(path) * (10 ** (level_gain_db(path, target) / 20) if target is not None else 1.0)


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
    assert movie == pytest.approx(180 + END_CARD_S, abs=1.0)                # cards: auto adds శుభం / THE END after it
    assert json.loads((root / "reports" / "sync.json").read_text())["ok"] is True   # every line over its own shots
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
        line = _levelled(Path(paths[c["line_id"]]), cfg)
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


def test_a_line_longer_than_its_shots_stops_the_video_step_before_lip_sync(tmp_path):
    """A voiced line that would run on past the shots carrying it (over the next scene's faces) is never placed
    there: the video step stops before lip-sync, naming it with its measured length, so it can be shortened."""
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
    with pytest.raises(LinesTooLong) as stop:
        Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None).run(until="assemble")
    over = {o["block_id"]: o for o in stop.value.over}
    assert long_id in over and over[long_id]["seconds"] == pytest.approx(long_s)
    assert over[long_id]["room_s"] < long_s and stop.value.measured[long_id] == pytest.approx(long_s)
    assert not (root / "10_edit" / "timeline.json").exists()         # no cut is made from lines that cannot match


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


def test_every_cut_lands_on_the_film_frame_grid(tmp_path):
    """Two scenes of three 0.52 s shots at 24 fps. Rounded one by one each shot is 12 frames, so the second scene
    would start at frame 36 and the picture would run ever earlier than the sound (which sits at exact times). On the
    grid every cut is at round(start * fps): 12, 25, 37, 50, 62, and the film is 75 frames."""
    from nraiorbit.post import assemble
    colours = ["red", "lime", "blue", "yellow", "magenta", "cyan"]
    refs, shots, t = [], [], 0.0
    for i, c in enumerate(colours):
        src = tmp_path / f"src_{c}.mp4"
        ffmpeg.run(["-f", "lavfi", "-i", f"color=c={c}:s=64x36:r=24", "-frames:v", "24", *ffmpeg.VIDEO_ENCODE, str(src)])
        clip = ffmpeg.conform_clip(src, tmp_path / f"{c}.mp4", 0.52, (64, 36), 24)
        assert ffmpeg.frame_count(clip) == 12
        shots.append(assemble.TimelineShot(f"s{i}", f"sc{i // 3}", round(t, 3), 0.52, clip))
        refs.append(_rgb(clip)[0])
        t += 0.52
    scenes = [assemble.render_scene(shots[:3], [], 0.0, tmp_path / "sc0.mp4", fps=24),
              assemble.render_scene(shots[3:], [], shots[3].start_s, tmp_path / "sc1.mp4", fps=24)]
    assemble.join_scenes(scenes, tmp_path / "film.mp4")
    frames = _rgb(tmp_path / ".film" / "video.mp4")
    assert len(frames) == round(t * 24) == 75
    shot_of = [int(np.argmin([np.abs(f - r).sum() for r in refs])) for f in frames]
    cuts = [k for k in range(1, len(shot_of)) if shot_of[k] != shot_of[k - 1]]
    assert cuts == [round(s.start_s * 24) for s in shots[1:]] == [12, 25, 37, 50, 62]
    assert sorted(set(shot_of)) == list(range(6))
    assert assemble.fit_frames(shots[0].clip, 12, tmp_path / "same.mp4") == shots[0].clip    # a fitting clip is reused


def test_a_shot_cut_into_later_shows_its_clip_on_the_frames_it_was_made_for(tmp_path):
    """A 1 s clip (frame k at grey 8 + 10k) lip-synced to start at 0.5 s; the edit then cuts into it 0.208 s later
    (in_s) and the red shot before it takes the time. Its clip's frame 0 still sits at film frame 12, so the cut at
    frame 17 opens on clip frame 5 and the lips stay on the voice, which keeps its time."""
    from nraiorbit.post import assemble
    red = tmp_path / "red.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "color=c=red:s=64x36:r=24", "-frames:v", "17", *ffmpeg.VIDEO_ENCODE, str(red)])
    ramp = _grey_clip(tmp_path / "ramp.mp4", [8 + 10 * k for k in range(24)])
    shots = [assemble.TimelineShot("s0", "sc0", 0.0, 0.708, red),
             assemble.TimelineShot("s1", "sc0", 0.708, 0.792, ramp, in_s=0.208)]
    assert assemble.lead_frames(shots[1], 24) == 5 and assemble.lead_frames(shots[0], 24) == 0
    assemble.render_scene(shots, [], 0.0, tmp_path / "sc0.mp4", fps=24)
    frames = _rgb(tmp_path / ".sc0" / "video.mp4")
    assert len(frames) == 36
    assert (frames[:17, 0] > 200).all() and (frames[:17, 1] < 60).all()          # red up to the cut at frame 17
    assert np.abs(frames[17:, 1] - [8 + 10 * k for k in range(5, 24)]).max() <= 4  # then clip frames 5..23


def test_a_cut_into_take_keeps_its_place_on_the_clock():
    """take_span: a shot the edit cuts into later (cut_in_s; the shot before it lengthened by as much) keeps its take's
    first frame and length as they were, so its lip-sync audio comes out sample for sample the same (the paid result
    is reused), also for lengths in whole frames as the shot planner writes them (145/24 s) and cut-ins of whole
    frames (5/24 s). The shown start and in_s give back that first frame exactly."""
    from nraiorbit.post import assemble
    for before in range(48, 121):                       # the shot before: every length from 2 to 5 s in frames
        for d in (115 / 24, 145 / 24, 4.8, 6.0):
            for h in (0.25, 5 / 24, 0.208333, 1 / 24, 0.123):
                origin, length = assemble.take_span(10.0 + before / 24, d)
                t = 10.0 + (before / 24 + h)
                o, n = assemble.take_span(t, d - h, h)
                assert o == origin and round(n * SR) == round(length * SR), (before, d, h)
                assert round(round(t, 3) - round(round(t, 3) - o, 3), 3) == origin
    assert assemble.take_span(12.5, 4.0, sync_s=4.8) == (12.5, 4.8)
    assert assemble.take_span(12.5, 4.8, sync_s=4.0) == (12.5, 4.8)


def test_cut_in_keeps_a_line_pulled_to_the_film_end_and_skips_a_scene_start(tmp_path):
    """cut_in_s on the film's last shot, whose line is pulled to end with the film: the line keeps its time, so the
    take and its lip-sync audio stay the same (nothing would be paid again). On a scene's first shot it is ignored and
    reported: a scene starts at a fixed time, so no shot before it can take the time, and its line stays put."""
    import hashlib

    from nraiorbit.providers.base import register
    from nraiorbit.providers.mock import MockLipSync
    from nraiorbit.schemas import Outline, SceneScript, ShotList

    sent = []

    @register("lipsync", "rec_cut_in")
    class Rec(MockLipSync):
        def __init__(self, **kw):
            pass

        def apply_many(self, items):
            sent.extend(items)
            return [self.apply(v, a, o) for v, a, o in items]

    root, cfg = _project(tmp_path)
    pipeline = Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None)
    pipeline.run(until="shots")
    scenes = Outline.model_validate_json((root / "02_outline.json").read_text()).scenes

    def give_line(scene_id, index):                 # the scene's last line goes to one shot, its speaker in frame
        path = root / "04_shots" / f"{scene_id}.json"
        shots = ShotList.model_validate_json(path.read_text())
        script = SceneScript.model_validate_json((root / "03_scenes" / f"{scene_id}.json").read_text())
        line = next(b for b in reversed(script.blocks) if b.kind == "dialogue")
        for s in shots.shots:
            s.block_ids = [b for b in s.block_ids if b != line.block_id]
        shot = shots.shots[index]
        shot.block_ids.append(line.block_id)
        shot.characters_in_frame = list(dict.fromkeys(shot.characters_in_frame + [line.character_id]))
        return path, shots, shot

    path, shots, end = give_line(scenes[-1].scene_id, -1)
    prev = shots.shots[-2]
    prev.duration_s, end.duration_s = round(prev.duration_s + end.duration_s - 2.0, 3), 2.0   # line runs past the end
    path.write_text(shots.model_dump_json(indent=2))
    first_path, first_shots, first = give_line(scenes[1].scene_id, 0)
    first_path.write_text(first_shots.model_dump_json(indent=2))
    cfg["providers"]["lipsync"] = "rec_cut_in"
    cfg["lipsync"] = {**cfg.get("lipsync", {}), "speaker_lock": False, "shots": {}}
    pipeline.run(until="render", force=("render",))

    def key(shot_id):
        video, audio, _ = next(i for i in sent if Path(i[0]).parent.name == shot_id)
        return hashlib.sha1(Path(video).read_bytes() + Path(audio).read_bytes()).hexdigest()

    def timeline():
        tl = json.loads((root / "10_edit" / "timeline.json").read_text())
        return {s["shot_id"]: s for s in tl["shots"]}, {e["ref_id"]: e["start_s"] for e in tl["dialogue"]}

    keys, (was, lines) = {s: key(s) for s in (end.shot_id, first.shot_id)}, timeline()
    assert lines[end.block_ids[-1]] + 4.0 > 180 > was[end.shot_id]["start_s"] + 0.2 + 1e-6 > lines[end.block_ids[-1]]
    sent.clear()
    prev.duration_s, end.duration_s = round(prev.duration_s + 0.25, 3), round(end.duration_s - 0.25, 3)
    path.write_text(shots.model_dump_json(indent=2))
    cfg["lipsync"]["shots"] = {end.shot_id: {"cut_in_s": 0.25}, first.shot_id: {"cut_in_s": 0.25}}
    pipeline.run(until="render", force=("render",))

    now, lines_now = timeline()
    assert lines_now == lines                                   # every line where it was
    assert {s: key(s) for s in keys} == keys                    # the same takes and lip-sync audio
    assert now[end.shot_id]["start_s"] == pytest.approx(was[end.shot_id]["start_s"] + 0.25)
    assert now[end.shot_id]["in_s"] == 0.25
    assert now[first.shot_id]["in_s"] == 0.0 and now[first.shot_id]["start_s"] == was[first.shot_id]["start_s"]
    queue = [json.loads(x) for x in (root / "reports" / "review_queue.jsonl").read_text().splitlines()]
    assert any(q["shot_id"] == first.shot_id and "cut_in_s ignored" in q["reason"][0] for q in queue)


def _grey_clip(out, levels):
    """A 64x36 clip at 24 fps whose frame k is flat grey levels[k]."""
    raw = np.repeat(np.array(levels, np.uint8)[:, None, None, None], 36 * 64 * 3).tobytes()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "64x36", "-r", "24",
                    "-i", "-", *ffmpeg.VIDEO_ENCODE, str(out)], input=raw, check=True)
    return out


def test_the_picture_sets_the_length_when_the_sound_comes_out_short(tmp_path):
    """Every AAC pass loses a few milliseconds at the end, and a remux with -shortest then cut the picture to the sound
    (the master lost the last 0.2 s of its end card). Short sound is padded with silence; every frame stays."""
    from nraiorbit.post import assemble
    video = tmp_path / "v.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "color=c=gray:s=64x36:r=24", "-frames:v", "240", *ffmpeg.VIDEO_ENCODE, str(video)])
    t = np.arange(int(9.5 * SR)) / SR
    film = ffmpeg.mux(video, _write_wav(tmp_path / "short.wav", 0.2 * np.sin(2 * np.pi * 300 * t)), tmp_path / "f.mp4")
    assert ffmpeg.frame_count(film) == 240 and ffmpeg.duration(film) >= 9.99      # 9.5 s of sound under 10 s of picture
    music = _write_wav(tmp_path / "music.wav", 0.1 * np.sin(2 * np.pi * 500 * np.arange(10 * SR) / SR))
    master = assemble.master(film, [assemble.AudioEvent(music, 0.0, 10.0, "music")], tmp_path / "master.mp4")
    assert ffmpeg.frame_count(master) == 240


def _rgb(video):
    """The centre pixel of every frame, as ints."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 36, 64, 3)[:, 18, 32].astype(int)


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


def test_billable_grid_puts_every_shot_on_a_veo_length(tmp_path):
    """shots.billable_grid with Veo: even scene targets, shots of 4, 6 or 8 s adding up to them (or a shot_issues
    row when a scene's shot count cannot meet it). Off, or without Veo, the shot lists are fitted as before."""
    import yaml

    from nraiorbit.config import deep_merge, load_pipeline_config
    from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
    from nraiorbit.pipeline import Pipeline
    from nraiorbit.schemas import Outline, ShotList

    def plan(extra):
        root = tmp_path / ("grid" if extra else "plain")
        (root / "input").mkdir(parents=True)
        (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
        cfg = deep_merge(DEMO_CONFIG, extra)
        (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        p = Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda *_: None)
        p.run(until="shots")
        return root

    plain = plan({})
    grid = plan({"providers": {"video": "veo"}, "shots": {"billable_grid": True}})
    outline = Outline.model_validate_json((grid / "02_outline.json").read_text())
    assert all(sc.target_seconds % 2 == 0 for sc in outline.scenes)
    assert sum(sc.target_seconds for sc in outline.scenes) == \
        sum(sc.target_seconds for sc in Outline.model_validate_json((plain / "02_outline.json").read_text()).scenes)
    issues = [json.loads(line) for line in (grid / "reports" / "shot_issues.jsonl").read_text().splitlines()] \
        if (grid / "reports" / "shot_issues.jsonl").exists() else []
    fell_back = {i["scene_id"] for i in issues if "billable_grid" in i}
    on_grid = 0
    for sc in outline.scenes:
        shots = ShotList.model_validate_json((grid / "04_shots" / f"{sc.scene_id}.json").read_text()).shots
        total = sum(s.duration_s for s in shots)
        assert total == pytest.approx(sc.target_seconds, abs=1 / 24)
        if sc.scene_id not in fell_back:
            assert all(s.duration_s in (4.0, 6.0, 8.0) for s in shots), sc.scene_id
            on_grid += 1
    assert on_grid >= 1
    off = [s for p in sorted((plain / "04_shots").glob("*.json")) for s in ShotList.model_validate_json(p.read_text()).shots]
    assert any(s.duration_s not in (4.0, 6.0, 8.0) for s in off)          # proportional fitting, as before
