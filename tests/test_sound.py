"""A film's sound effects from the studio's library: a quiet bed of ambience under each scene (its weather, else its
place, else the night) and sounds that belong on cuts (a boom under slow motion, a whoosh into a leap, thunder on
lightning), never a hit placed blind; decided once per cut and kept, and never added to a cut approved before."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from nraiorbit.post import ffmpeg, sound


def scene(sid, heading="EXT. VILLAGE - DAY", place="", time="morning", synopsis=""):
    return N(scene_id=sid, heading=heading, location_hint=place, time_of_day=time, synopsis=synopsis)


def shot(sid, motion="", image="", cues=()):
    return N(shot_id=sid, motion_prompt=motion, image_prompt=image, sfx_cues=list(cues))


ALL = {a[0] for a in sound.AMBIENCE} | {name for name, _ in sound.AT_CUT} | {sound.NIGHT}


def test_each_scene_gets_its_weather_else_its_place_else_the_night_and_words_are_whole():
    assert sound.scene_bed(scene("a", place="riverbank", synopsis="monsoon rain lashes the ghat")) == "rain_heavy"
    assert sound.scene_bed(scene("b", heading="EXT. NALLAMALA FOREST - DUSK")) == "forest_birds"
    assert sound.scene_bed(scene("c", heading="EXT. BACKYARD - NIGHT", time="night")) == "night_crickets"
    assert sound.scene_bed(scene("c2", heading="INT. HOUSE - NIGHT", time="night")) is None        # indoors: quiet
    assert sound.scene_bed(scene("d", heading="INT. OFFICE", synopsis="a brainstorm about the train")) is None
    assert sound.at_cut(shot("s", motion="Chintu leaps in slow motion across the gorge")) == "slowmo_boom"
    assert sound.at_cut(shot("s", motion="he leaps onto the rock")) == "whoosh_fast"
    assert sound.at_cut(shot("s", motion="he punches the smuggler")) is None          # a hit is never placed blind
    assert sound.at_cut(shot("s", motion="a trainee walks")) is None
    assert sound.at_cut(shot("s", motion="he turns", cues=["punch_heavy", "whoosh"])) == "whoosh_fast"   # the director's
    for wrong in (scene("e", heading="INT. HOUSE - DAY", synopsis="Krishna confronts his father"),
                  scene("f", heading="INT. VILLAGE HEAD HOUSE - NIGHT", time="night"),
                  scene("g", heading="EXT. YARD - NOON", time="noon", synopsis="they open fire on Raju and storm out"),
                  scene("h", heading="EXT. PADDY FIELDS - NIGHT", time="night", synopsis="the village sleeps")):
        assert sound.scene_bed(wrong) in (None, "night_crickets"), wrong      # no river, roosters, fire or wind
    assert sound.scene_bed(scene("i", heading="EXT. KRISHNA GHAT - DAWN", time="dawn")) == "river_flow"
    for motion in ("a lightning-fast kick", "music blasts from the speaker", "camera holds on the jeep"):
        assert sound.at_cut(shot("s", motion=motion)) in (None, "jeep_pass")
    assert sound.at_cut(shot("s", motion="a lightning-fast kick")) is None
    assert sound.at_cut(shot("s", image="an old jeep parked by the hut")) is None    # the picture is silent


def test_the_plan_uses_only_what_the_library_has_and_a_run_of_slow_motion_gets_one_boom():
    scenes = [(scene("sc001", place="village"), 0.0, 20.0), (scene("sc002", heading="INT. HUT - NIGHT", time="night"),
                                                              20.0, 2.0)]
    shots = [{"shot_id": "s1", "scene_id": "sc001", "start_s": 4.0, "duration_s": 3.0},
             {"shot_id": "s2", "scene_id": "sc001", "start_s": 7.0, "duration_s": 3.0},
             {"shot_id": "s3", "scene_id": "sc001", "start_s": 10.0, "duration_s": 3.0},
             {"shot_id": "s4", "scene_id": "sc001", "start_s": 13.0, "duration_s": 3.0, "real": False}]
    plans = {"s1": shot("s1", "slow motion jump"), "s2": shot("s2", "he lands in slow motion"),
             "s3": shot("s3", "the hero leaps"), "s4": shot("s4", "slow motion")}
    planned = sound.plan_sound(scenes, shots, plans, ALL)
    assert [(p["kind"], p["name"], p["start_s"]) for p in planned] == [
        ("bed", "village_morning", 0.0), ("cut", "slowmo_boom", 4.0), ("cut", "whoosh_fast", 9.75)]
    # the 2 s night scene is too short for a bed; the second slow-motion shot of a run, and a placeholder, get none
    assert sound.plan_sound(scenes, shots, plans, {"slowmo_boom"}) == [planned[1]]   # no village sound: no bed


def _film(tmp_path, monkeypatch, feed_dir):
    root = tmp_path / "rain"
    (root / "reports").mkdir(parents=True)
    feed_dir.mkdir(parents=True, exist_ok=True)
    for name in ("forest_birds", "slowmo_boom"):
        (feed_dir / f"{name}_sfx_20261025-220000.mp3").write_bytes(name.encode())
    entries = [{"mood": n, "path": feed_dir / f"{n}_sfx_20261025-220000.mp3", "created": "2026-10-25"}
               for n in ("forest_birds", "slowmo_boom")]
    monkeypatch.setattr(sound, "looped", lambda src, seconds, out: out.parent.mkdir(parents=True, exist_ok=True)
                        or out.write_bytes(src.read_bytes() + f"x{seconds:g}".encode()) and out)
    scenes = [(scene("sc001", heading="EXT. FOREST - DAY"), 0.0, 30.0)]
    shots = [{"shot_id": "s1", "scene_id": "sc001", "start_s": 6.0, "duration_s": 4.0}]
    plans = {"s1": shot("s1", "the monkey falls in slow motion")}
    return root, entries, scenes, shots, plans


def test_a_films_sound_is_decided_once_kept_in_its_own_cache_and_never_added_to_an_approved_cut(tmp_path, monkeypatch):
    feed_dir = tmp_path / "feed"
    root, entries, scenes, shots, plans = _film(tmp_path, monkeypatch, feed_dir)
    assert sound.film_sound(root, scenes, shots, plans, [], tmp_path / "w0", log=lambda *_: None) == []
    assert not (root / "reports" / "sound_plan.json").exists()               # an empty library decides nothing
    events = sound.film_sound(root, scenes, shots, plans, entries, tmp_path / "work", log=lambda *_: None,
                              extra_db=2.0)
    assert [(Path(p).name, start, gain) for p, start, gain in events] == [
        ("bed_sc001.m4a", 0.0, sound.AMBIENCE_DB + 2.0), ("slowmo_boom.mp3", 6.0, sound.AT_CUT_DB + 2.0)]
    assert sorted(p.name for p in (root / "cache" / "sfx").iterdir()) == ["forest_birds.mp3", "slowmo_boom.mp3"]
    plan = json.loads((root / "reports" / "sound_plan.json").read_text())
    for f in feed_dir.iterdir():                                            # the library loses them later
        f.unlink()
    again = sound.film_sound(root, scenes, shots, {}, [], tmp_path / "work2", log=lambda *_: None)
    assert [Path(p).name for p, _, _ in again] == ["bed_sc001.m4a", "slowmo_boom.mp3"]   # the same, from its cache
    assert json.loads((root / "reports" / "sound_plan.json").read_text()) == plan
    recut = [{**shots[0], "start_s": 9.0}]                                   # a re-cut: planned for its new times
    moved = sound.film_sound(root, scenes, recut, plans, [], tmp_path / "work3", log=lambda *_: None)
    assert [start for _, start, _ in moved] == [0.0, 9.0]
    old = tmp_path / "old"
    old_root, entries2, scenes2, shots2, plans2 = _film(old, monkeypatch, tmp_path / "feed2")
    (old_root / "reviews").mkdir()
    (old_root / "reviews" / "final_cut.approved").write_text("ok")          # approved before sound effects
    assert sound.film_sound(old_root, scenes2, shots2, plans2, entries2, tmp_path / "w3", log=lambda *_: None) == []
    assert json.loads((old_root / "reports" / "sound_plan.json").read_text())["events"] == []   # and stays so


@pytest.mark.skipif(not ffmpeg.available(), reason="needs ffmpeg")
def test_a_scenes_bed_is_looped_to_its_length_with_soft_edges(tmp_path):
    src = tmp_path / "birds.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:d=3", str(src)],
                   check=True)
    out = sound.looped(src, 20.0, tmp_path / "bed.m4a")
    assert abs(ffmpeg.duration(out) - 20.0) < 0.1
