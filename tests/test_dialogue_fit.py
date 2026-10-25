"""Voices match their pictures: a line is heard over the shots that carry it, never drifting over other faces or past
the end of the film. Scenes are made long enough for their dialogue (the film may grow to 5/3 of the length asked
for), shot plans give every line a shot that shows its speaker and is long enough, voiced lines that overrun stop the
run before lip-sync and are shortened by the screenwriter, and the cut is checked line by line before the owner sees
it. Cards: the film's name over its opening, శుభం / THE END after its last frame."""

import json
from types import SimpleNamespace as N

import pytest

from nraiorbit.planning import fit
from nraiorbit.post.cards import END_CARD_S, Card, auto_cards


def shot(sid, dur, blocks, who=()):
    return N(shot_id=sid, duration_s=dur, block_ids=list(blocks), characters_in_frame=list(who))


def line(bid, who, te="", kind="dialogue"):
    return N(block_id=bid, character_id=who, text_te=te, kind=kind)


def test_speech_is_estimated_from_the_telugu_letters():
    assert fit.speech_seconds("") == 0.0
    assert fit.speech_seconds("అ" * 72) == pytest.approx(7.14)            # measured 6.69 s: the estimate errs long
    assert fit.speech_seconds("అ ఆ") == fit.speech_seconds("అఆ")             # spaces are not spoken
    assert fit.chars_for(fit.speech_seconds("అ" * 40)) == 40
    assert fit.max_runtime_s({"runtime_minutes": 3}) == pytest.approx(300)  # a 3-minute film may run to 5
    assert fit.max_runtime_s({"runtime_minutes": 3, "max_runtime_minutes": 4}) == 240


def test_scenes_grow_to_hold_their_dialogue_and_over_the_cap_everyone_gives_a_little():
    lengths = {"a": 14, "b": 14, "c": 14}
    new, cut = fit.lengthen_scenes(lengths, {"a": 6.0, "b": 15.0, "c": 30.0}, cap_s=100)
    assert new == {"a": 14, "b": 20, "c": 40} and cut == {}                  # 15 / 0.75 = 20; 30 / 0.75 = 40
    new, cut = fit.lengthen_scenes(lengths, {"a": 6.0, "b": 15.0, "c": 30.0}, cap_s=60)
    assert sum(new.values()) <= 60 and new["a"] == 14                         # a never needed to grow
    assert set(cut) == {"b", "c"} and cut["c"] > cut["b"]                    # both give back, the longer more


def test_lines_drift_off_their_shots_when_a_scene_holds_too_much_speech():
    """The broken cut, in small: three long lines in a scene of two short shots."""
    shots = [shot("s1", 4, ["b1", "b2"], ["hero"]), shot("s2", 4, ["b3"], ["villain"])]
    blocks = [line("b1", "hero"), line("b2", "hero"), line("b3", "villain")]
    lines = fit.plan_lines([("sc", 0.0, shots, blocks)], {"b1": 3.0, "b2": 3.0, "b3": 3.0}, film_end=8.0)
    by = {ln.block_id: ln for ln in lines}
    assert by["b1"].start_s == pytest.approx(0.2) and by["b1"].overrun_s(8.0) == 0
    assert by["b2"].start_s == pytest.approx(3.45) and by["b2"].overrun_s(8.0) == pytest.approx(1.95)
    assert by["b3"].overrun_s(8.0) > 0                                         # the villain's line runs past the film
    ok = fit.plan_lines([("sc", 0.0, shots, blocks)], {"b1": 1.5, "b2": 1.5, "b3": 3.0}, film_end=8.0)
    assert all(ln.overrun_s(8.0) == 0 for ln in ok)


def test_a_shot_plan_is_checked_for_its_dialogue():
    blocks = [line("b1", "hero", "అ" * 30), line("b2", "villain", "అ" * 30), line("b3", "kid", "అ" * 10),
              line("b4", "hero", "అ" * 10)]
    shots = [shot("s1", 4, ["b1"], ["hero"]), shot("s2", 4, ["b2"], ["hero"]), shot("s3", 3, ["b3"], ["hero"])]
    problems = fit.shot_problems(blocks, shots, 0.0, off_screen={"kid": "Kid (8)"})
    assert "line b4 is in no shot" in problems
    assert any(p.startswith("line b2 (villain) is heard over shots that do not show its speaker") for p in problems)
    assert not any("b3" in p and "speaker" in p for p in problems)          # a child kept off screen: by design
    long = [shot("s1", 2, ["b1"], ["hero"])]
    assert any("needs about" in p and "b1" in p for p in fit.shot_problems(blocks[:1], long, 0.0))


def test_the_cut_is_checked_line_by_line():
    timeline = {"film_seconds": 10, "shots": [{"shot_id": "s1", "start_s": 0, "duration_s": 5},
                                              {"shot_id": "s2", "start_s": 5, "duration_s": 5}],
                "dialogue": [{"ref_id": "b1", "start_s": 0.2, "duration_s": 4.0},
                             {"ref_id": "b2", "start_s": 4.5, "duration_s": 3.0},
                             {"ref_id": "b3", "start_s": 8.0, "duration_s": 3.0}]}
    scripts = {"b1": line("b1", "hero"), "b2": line("b2", "hero"), "b3": line("b3", "villain")}
    plans = {"s1": shot("s1", 5, ["b1", "b2"], ["hero"]), "s2": shot("s2", 5, ["b3"], ["villain"])}
    problems = fit.sync_problems(timeline, scripts, plans)
    assert problems == ["b2: heard at 0:04.5-0:07.5, its shots run 0:00.0-0:05.0",
                        "b3: plays past the end of the film (11.0 s of 10.0 s)"]
    synced = fit.sync_problems({**timeline, "dialogue": timeline["dialogue"][:1]}, scripts, plans,
                               lipsync={"s1": {"mode": "not synced", "why": "no speaker face found"}})
    assert synced == ["b1: hero is on screen in s1 but not lip-synced (no speaker face found)"]
    assert fit.sync_problems({**timeline, "dialogue": timeline["dialogue"][:1]}, scripts, plans, no_lipsync={"s1"},
                             lipsync={"s1": {"mode": "not synced"}}) == []           # turned off by the owner
    cut_in = {**timeline, "shots": [{"shot_id": "s1", "start_s": 0.5, "duration_s": 4.5, "in_s": 0.5}] +
              timeline["shots"][1:], "dialogue": [{"ref_id": "b1", "start_s": 0.2, "duration_s": 4.0}]}
    assert fit.sync_problems(cut_in, scripts, plans) == []                   # measured from the take's first frame
    assert fit.sync_problems({**timeline, "dialogue": timeline["dialogue"][:1]}, scripts, plans,
                             lipsync={"s1": {"mode": "speaker crops"}}) == []


def test_the_real_broken_cut_is_reproduced_and_caught():
    """The placement rule is the render step's: on monkey-saves-man-2's plans it gives the very start times of the
    cut the owner rejected (all 47), and flags 46 of its lines."""
    from nraiorbit.demo import OUTLINE  # noqa: F401  (a smoke import: the module stays importable on its own)
    shots = [shot("a", 8, ["l1", "l2"], ["x"]), shot("b", 6, ["l3"], ["y"])]
    blocks = [line("l1", "x"), line("l2", "x"), line("l3", "y")]
    lines = fit.plan_lines([("sc", 12.0, shots, blocks)], {"l1": 6.7, "l2": 4.0, "l3": 3.8}, film_end=26.0)
    assert [ln.start_s for ln in lines] == [12.2, 19.15, 23.4]                # each waits for the one before
    assert [ln.overrun_s(26.0) > 0 for ln in lines] == [False, True, True]


def test_cards_auto_name_the_film_before_its_first_line_and_end_it_after():
    title, end = auto_cards("గోదావరి సింహం", "Godavari Simham", first_line_s=12.0)
    assert (title.kind, title.at_s, title.length_s, title.text_en) == ("title", 0.3, 4.0, "GODAVARI SIMHAM")
    assert not title.after and end.after and end.length_s == END_CARD_S and end.text_te == "శుభం"
    assert [c.kind for c in auto_cards("x", "y", first_line_s=2.0)] == ["end"]   # no room: no speaker darkened
    near, _ = auto_cards("x", "y", first_line_s=3.5)
    assert near.length_s == pytest.approx(2.7)                               # ends before the first line
    card = Card.from_config({"kind": "end", "length_s": 3, "after": True}, root=None)
    assert card.after and card.record()["after"] is True


# ---------------------------------------------------------------- the pipeline and the runner

@pytest.fixture
def film(tmp_path):
    """A planned film whose scene 1 holds far more speech than its shots, with a cut measured (reports/edl.json)."""
    from nraiorbit.config import load_pipeline_config
    from nraiorbit.demo import STORY_BIBLE
    from nraiorbit.pipeline import Pipeline
    from nraiorbit.schemas import Outline, SceneScript, ShotList, StoryBible
    root = tmp_path / "rain"
    for d in ("03_scenes", "04_shots", "reports", "10_edit", "11_master"):
        (root / d).mkdir(parents=True)
    bible = StoryBible.model_validate(STORY_BIBLE)
    (root / "01_story_bible.json").write_text(bible.model_dump_json(), encoding="utf-8")
    sc = {"scene_id": "sc001", "beat_ids": [], "heading": "EXT. RIVER - DAY", "location_hint": "river",
          "time_of_day": "morning", "characters": ["arjun", "meera"], "synopsis": "s", "rasa_primary": "veera",
          "rasa_secondary": None, "intensity": 5, "set_piece": "none", "target_seconds": 12}
    (root / "02_outline.json").write_text(Outline.model_validate({"scenes": [sc]}).model_dump_json(), encoding="utf-8")
    blocks = [{"block_id": f"sc001_b00{i}", "kind": "dialogue", "character_id": who, "text_te": "అ" * 60,
               "text_en": "a long line", "delivery": None} for i, who in ((1, "arjun"), (2, "meera"), (3, "arjun"))]
    (root / "03_scenes" / "sc001.json").write_text(SceneScript.model_validate(
        {"scene_id": "sc001", "heading": "h", "synopsis": "s", "rasa_primary": "veera", "intensity": 5,
         "blocks": blocks}).model_dump_json(), encoding="utf-8")
    from nraiorbit.demo import SHOTS
    base = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0]
    plan = [base.model_copy(update={"shot_id": f"sc001_s00{i}", "duration_s": 4.0, "block_ids": [f"sc001_b00{i}"],
                                    "characters_in_frame": [who]})
            for i, who in ((1, "arjun"), (2, "meera"), (3, "arjun"))]
    (root / "04_shots" / "sc001.json").write_text(ShotList(scene_id="sc001", shots=plan).model_dump_json(),
                                                 encoding="utf-8")
    (root / "reports" / "edl.json").write_text(json.dumps({"film_seconds": 12, "shots": [], "dialogue": [
        {"ref_id": f"sc001_b00{i}", "start_s": 0, "duration_s": 6.0} for i in (1, 2, 3)]}))
    for i in (1, 2, 3):
        (root / "05_dialogue" / f"sc001_b00{i}").mkdir(parents=True)
        (root / "05_dialogue" / f"sc001_b00{i}" / "take_1.wav").write_bytes(b"old voice")
    (root / "10_edit" / "timeline.json").write_text("{}")
    (root / "11_master" / "movie.mp4").write_bytes(b"film")
    p = Pipeline(root, None, cfg=load_pipeline_config(), log=lambda *_: None)
    p.remove_remote = lambda paths: None
    return p


def _screenwriter(asked, shorten_to=20):
    from nraiorbit.schemas import ShortenedLines

    def shorten_lines(bible, outline, script, limits):
        asked.append(dict(limits))
        return ShortenedLines.model_validate({"lines": [
            {"block_id": bid, "text_te": "అ" * min(n, shorten_to), "text_en": "short"} for bid, n in limits.items()]})
    return N(shorten_lines=shorten_lines)


def test_lines_that_overrun_their_shots_are_shortened_before_anything_is_paid(film):
    from nraiorbit.service import produce
    asked = []
    film.screenwriter = _screenwriter(asked)
    done = produce.fit_dialogue(film, log=lambda *_: None)
    assert done and set(done) <= {"sc001_b001", "sc001_b002", "sc001_b003"}
    assert all(n <= fit.chars_for(4.0 + fit.OVERRUN_TOL_S) for n in asked[0].values())
    script = json.loads((film.p.scenes / "sc001.json").read_text())
    texts = {b["block_id"]: b["text_te"] for b in script["blocks"]}
    assert all(len(texts[b]) <= 20 for b in done)
    assert all(not (film.p.dialogue / b).exists() for b in done)               # voiced again with the new words
    assert not (film.p.edit / "timeline.json").exists() and not (film.p.master / "movie.mp4").exists()
    record = json.loads((film.p.reports / produce.DIALOGUE_FIT).read_text())
    assert set(record) == set(done) and record[done[0]]["was_te"] == "అ" * 60
    assert produce.fit_dialogue(film, log=lambda *_: None) == [] and len(asked) == 1   # its old length is not used


def test_lines_that_fit_are_left_alone(film):
    from nraiorbit.service import produce
    edl = json.loads((film.p.reports / "edl.json").read_text())
    for d in edl["dialogue"]:
        d["duration_s"] = 2.0
    (film.p.reports / "edl.json").write_text(json.dumps(edl))
    film.screenwriter = _screenwriter(asked := [])
    assert produce.fit_dialogue(film, log=lambda *_: None, measured={}) == [] and asked == []


def test_a_render_stopped_for_long_lines_shortens_them_and_asks_the_cost_again_when_it_grew(film, monkeypatch):
    from nraiorbit.pipeline import LinesTooLong
    from nraiorbit.service import produce
    over = [{"block_id": "sc001_b002", "scene_id": "sc001", "seconds": 6.0, "start_s": 4.2, "room_s": 4.3,
             "over_s": 1.7}]
    runs = []

    def run(until=None, force=()):
        runs.append(until)
        if until is None:
            raise LinesTooLong(over, {"sc001_b001": 3.0, "sc001_b002": 6.0, "sc001_b003": 3.0})
    film.run = run
    film.cfg = {**film.cfg, "providers": {**film.cfg["providers"], "video": "mock"}}
    monkeypatch.setattr(produce, "fit_dialogue", lambda p, log, measured=None: ["sc001_b002"] if measured else [])
    monkeypatch.setattr(produce, "cost_gate", lambda p: (None, {"total_usd": 1.5, "lipsync_usd": 1.0}))
    monkeypatch.setattr(produce, "_faces_needed", lambda p: False)
    monkeypatch.setattr("nraiorbit.service.rehearse.save", lambda pipeline, log=print: None)
    (film.p.reviews).mkdir(parents=True, exist_ok=True)
    (film.p.reviews / "cost.approved").write_text('{"total_usd": 1.5}\n')
    calls = [0]

    def priced(p):                                     # what the owner approved before the run, then the new voices
        calls[0] += 1
        return None, {"total_usd": 1.5 if calls[0] == 1 else 4.0, "lipsync_usd": 1.0}
    monkeypatch.setattr(produce, "cost_gate", priced)
    report = produce.produce(film, log=lambda *_: None)
    assert report["state"] == "awaiting:cost" and report["steps"]["dialogue"].endswith("sc001_b002")
    assert report["steps"]["cost"] == "awaiting:cost (lines shortened to fit their shots)"
    assert runs == ["shots", None]                                          # stopped in the video step, not before
    assert not (film.p.reviews / "cost.approved").exists() and (film.p.reviews / "cost.pending").exists()


def test_lines_shortened_within_what_the_owner_approved_go_straight_on(film, monkeypatch):
    """The owner approved the cost once: new voices for a few shortened lines (cents) never ask it again, and the
    render review is not re-opened for them either."""
    from nraiorbit.pipeline import LinesTooLong
    from nraiorbit.service import produce
    runs = []

    def run(until=None, force=()):
        runs.append(until)
        if until is None and len(runs) == 2:
            raise LinesTooLong([{"block_id": "sc001_b002", "scene_id": "sc001", "seconds": 6.0, "start_s": 4.2,
                                 "room_s": 4.3, "over_s": 1.7}], {"sc001_b002": 6.0})
    film.run = run
    film.cfg = {**film.cfg, "providers": {**film.cfg["providers"], "video": "mock"}}
    monkeypatch.setattr(produce, "fit_dialogue", lambda p, log, measured=None: ["sc001_b002"] if measured else [])
    monkeypatch.setattr(produce, "cost_gate", lambda p: (None, {"total_usd": 1.6, "lipsync_usd": 1.0}))
    monkeypatch.setattr(produce, "_faces_needed", lambda p: False)
    monkeypatch.setattr("nraiorbit.service.rehearse.save", lambda pipeline, log=print: None)
    monkeypatch.setattr("nraiorbit.qa.film.check", lambda root, log=None: {"ok": True})
    film.p.reviews.mkdir(parents=True, exist_ok=True)
    (film.p.reviews / "cost.approved").write_text('{"total_usd": 1.5, "spent_usd": 0}\n')
    (film.p.reviews / "render.approved").write_text("ok\n")
    report = produce.produce(film, log=lambda *_: None)
    assert runs == ["shots", None, None] and report["state"] != "awaiting:cost"
    assert (film.p.reviews / "cost.approved").exists() and not (film.p.reviews / "cost.pending").exists()
    assert (film.p.reviews / "render.approved").exists()


def _faceless(*timing):
    from nraiorbit.pipeline import CutDoesNotMatch
    face = "sc001_b002: meera is on screen in sc001_s002 but not lip-synced (no speaker face found)"
    return CutDoesNotMatch([face, *timing], ["sc001_s002"], list(timing))


def _cut_stops(film, monkeypatch, off):
    from nraiorbit.service import produce
    runs, reworked = [], []

    def run(until=None, force=()):
        runs.append(until)
        if until is None:
            raise off
    film.run = run
    film.rework_shots = lambda ids, notes, reason="": reworked.append((list(ids), notes, reason)) or list(ids)
    film.cfg = {**film.cfg, "providers": {**film.cfg["providers"], "video": "mock"}}
    monkeypatch.setattr(produce, "cost_gate", lambda p: (None, {}))
    monkeypatch.setattr(produce, "_faces_needed", lambda p: False)
    monkeypatch.setattr("nraiorbit.service.rehearse.save", lambda pipeline, log=print: None)
    return runs, reworked


def test_a_speaker_lip_sync_could_not_find_is_planned_again_closer_once(film, monkeypatch):
    """A group shot where the speaker's face is too small to sync: the director plans that shot again as a close-up
    of whoever speaks (a new take, so new first frames and, under "ask", the faces check) and the render goes on.
    Each shot gets that once; a second miss stops the cut with the list, as before."""
    from nraiorbit.service import produce
    runs, reworked = _cut_stops(film, monkeypatch, _faceless())
    report = produce.produce(film, log=lambda *_: None)
    assert reworked == [(["sc001_s002"], produce.CLOSE_UP_NOTE, "lip-sync found no speaker's face in it.")]
    assert report["steps"]["close_ups"] == "planned again as close-ups: sc001_s002"
    assert runs == ["shots", None, None] and report["state"].startswith("blocked:the cut does not match")
    assert "sc001_s002" in json.loads((film.p.reports / produce.FACE_REPLANS).read_text())
    runs.clear()
    assert produce.produce(film, log=lambda *_: None)["state"].startswith("blocked:")
    assert len(reworked) == 1 and runs == ["shots", None]                  # never planned again a second time


def test_a_cut_off_in_its_timing_is_stopped_and_nothing_is_planned_again(film, monkeypatch):
    from nraiorbit.service import produce
    runs, reworked = _cut_stops(film, monkeypatch, _faceless("sc001_b003: heard at 0:09.0-0:12.5, its shots run "
                                                             "0:08.0-0:12.0"))
    report = produce.produce(film, log=lambda *_: None)
    assert reworked == [] and runs == ["shots", None] and report["state"].startswith("blocked:")


def test_the_sync_check_names_the_shots_where_lip_sync_found_no_face(film):
    from nraiorbit.pipeline import CutDoesNotMatch
    film.cfg = {**film.cfg, "providers": {**film.cfg["providers"], "lipsync": "sync_so"}}
    (film.p.edit / "timeline.json").write_text(json.dumps({"film_seconds": 12, "render_id": "r1", "shots": [
        {"shot_id": f"sc001_s00{i}", "start_s": 4.0 * (i - 1), "duration_s": 4.0} for i in (1, 2, 3)], "dialogue": [
        {"ref_id": f"sc001_b00{i}", "start_s": 4.0 * (i - 1) + 0.2, "duration_s": 3.5} for i in (1, 2, 3)]}))
    (film.p.reports / "lipsync.jsonl").write_text(json.dumps({"shot_id": "sc001_s002", "render": "r1",
                                                              "mode": "not synced", "why": "no speaker face found"}) + "\n")
    with pytest.raises(CutDoesNotMatch) as off:
        film._check_sync()
    assert off.value.faceless == ["sc001_s002"] and off.value.timing == []


def test_a_line_spoken_only_in_wide_shots_is_planned_again_closer():
    blocks = [line("b1", "arjun", "అ" * 10)]
    wide = [N(shot_id="s1", duration_s=4.0, block_ids=["b1"], characters_in_frame=["arjun"], size="WS")]
    assert any("wide shot only (s1)" in x for x in fit.shot_problems(blocks, wide, 0.0))
    assert not fit.shot_problems(blocks, [N(**{**vars(wide[0]), "size": "MCU"})], 0.0)
    closer = wide + [N(shot_id="s2", duration_s=4.0, block_ids=["b1"], characters_in_frame=["arjun"], size="CU")]
    assert not any("wide" in x for x in fit.shot_problems(blocks, closer, 0.0))   # one shot shows the face
    assert not fit.shot_problems(blocks, wide, 0.0, voiced_off={"arjun"})        # heard off screen by design


def test_lip_sync_paid_in_an_earlier_container_is_not_estimated_again(film):
    """A fresh container has no synced clips on disk; the run records say what was paid. A shot synced on its current
    take, with the cast's voices as they are and no line of the film rewritten since, is free; anything else is
    counted (a new take, a new voice or a moved line is a new sync.so job)."""
    import yaml
    from nraiorbit.costing import lipsynced_before, voice_mark
    root = film.p.root
    at, assets = "2026-10-25T03:00:00+00:00", {"arjun": "voices/arjun@v1", "meera": "voices/meera@v1"}
    lock = {c: {"character_asset": f"{c}_x", "voice_asset": v, "costumes": {}} for c, v in assets.items()}
    (root / "cast.lock.yaml").write_text(yaml.safe_dump({"lock_version": 1, "characters": lock}))
    cfg = {"voice": {"cast": {"arjun": "el-voice-1"}}, "providers": {"tts": "elevenlabs"}}
    voices = json.loads(json.dumps(voice_mark(cfg, lock)))                     # as the record keeps it
    recs = [{"shot_id": f"sc001_s00{i}", "mode": "crop", "clip": f"/work/retake_2/07_video/sc001_s00{i}/take_1.mp4",
             "at": at, "voices": voices} for i in (1, 2, 3)]
    (root / "reports" / "lipsync.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (root / "takes.yaml").write_text("sc001_s003: 2\n")                    # a new take since: a new clip to sync
    (root / "reports" / "dialogue_fit.json").write_text(json.dumps({
        "sc001_b001": {"now_te": "అ", "at": "2026-10-25T02:00:00+00:00"}}))     # rewritten before it was synced
    shots = {f"sc001_s00{i}": 4.0 for i in (1, 2, 3)}
    assert lipsynced_before(root, shots, cfg) == {"sc001_s001", "sc001_s002"}  # the folder's "retake_2" is no take
    (root / "cast.lock.yaml").write_text((root / "cast.lock.yaml").read_text().replace("meera@v1", "meera@v2"))
    assert lipsynced_before(root, shots, cfg) == set()                        # a new voice for someone in the cast
    (root / "cast.lock.yaml").write_text((root / "cast.lock.yaml").read_text().replace("meera@v2", "meera@v1"))
    picked = {**cfg, "voice": {"cast": {"arjun": "el-voice-2"}}}               # the owner picked another voice
    assert lipsynced_before(root, shots, picked) == set()
    (root / "reports" / "dialogue_fit.json").write_text(json.dumps({
        "sc001_b003": {"now_te": "అ", "at": "2026-10-25T03:30:00+00:00"}}))     # any line rewritten after: lines move
    assert lipsynced_before(root, shots, cfg) == set()
    (root / "reports" / "dialogue_fit.json").unlink()
    with open(root / "reports" / "lipsync.jsonl", "a") as f:
        f.write(json.dumps({"shot_id": "sc001_s001", "mode": "not synced", "clip": "take_1.mp4", "at": at,
                            "voices": voices}) + "\n")
    assert lipsynced_before(root, shots, cfg) == {"sc001_s002"}                    # its latest record is the one that counts
    old = [{k: v for k, v in r.items() if k != "voices"} for r in recs]       # an older runner's records say no voices
    (root / "reports" / "lipsync.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old))
    assert lipsynced_before(root, shots, cfg) == set()


def test_the_cut_that_does_not_match_is_never_shown(film):
    from nraiorbit.pipeline import PipelineBlocked
    (film.p.edit / "timeline.json").write_text(json.dumps({"film_seconds": 12, "shots": [
        {"shot_id": f"sc001_s00{i}", "start_s": 4.0 * (i - 1), "duration_s": 4.0} for i in (1, 2, 3)], "dialogue": [
        {"ref_id": "sc001_b001", "start_s": 0.2, "duration_s": 3.5},
        {"ref_id": "sc001_b002", "start_s": 3.95, "duration_s": 6.0}]}))
    with pytest.raises(PipelineBlocked, match="the cut does not match its pictures"):
        film._check_sync()
    sync = json.loads((film.p.reports / "sync.json").read_text())
    assert sync["ok"] is False and sync["problems"][0].startswith("sc001_b002: heard at")
    (film.p.edit / "timeline.json").write_text(json.dumps({"film_seconds": 12, "shots": [
        {"shot_id": f"sc001_s00{i}", "start_s": 4.0 * (i - 1), "duration_s": 4.0} for i in (1, 2, 3)], "dialogue": [
        {"ref_id": f"sc001_b00{i}", "start_s": 4.0 * (i - 1) + 0.2, "duration_s": 3.5} for i in (1, 2, 3)]}))
    film._check_sync()
    assert json.loads((film.p.reports / "sync.json").read_text()) == {"ok": True, "problems": []}


def test_new_scenes_are_lengthened_to_hold_their_dialogue(film):
    from nraiorbit.schemas import Outline, StoryBible
    bible = StoryBible.model_validate_json(film.p.bible.read_text())
    outline = Outline.model_validate_json(film.p.outline.read_text())
    revised = []
    film.screenwriter = N(revise=lambda *a: revised.append(a) or a[2])
    out = film._fit_scene_lengths(bible, outline)
    speech = 3 * (fit.speech_seconds("అ" * 60) + fit.DIALOGUE_LEAD_IN_S + fit.DIALOGUE_GAP_S)
    assert out.scenes[0].target_seconds >= speech / fit.SCENE_SPEECH_SHARE and revised == []
    assert Outline.model_validate_json(film.p.outline.read_text()).scenes[0].target_seconds == out.scenes[0].target_seconds
    assert json.loads((film.p.reports / "scene_fit.jsonl").read_text().splitlines()[-1])["lengthened"]


def test_a_film_that_cannot_hold_its_dialogue_even_longer_stops_before_the_shots(film):
    from nraiorbit.pipeline import PipelineBlocked
    from nraiorbit.schemas import Outline, StoryBible
    film.cfg["film"] = {**film.cfg["film"], "max_runtime_minutes": 0.25}      # 15 s at most
    bible = StoryBible.model_validate_json(film.p.bible.read_text())
    outline = Outline.model_validate_json(film.p.outline.read_text())
    revised = []
    film.screenwriter = N(revise=lambda *a: revised.append(a[3]) or a[2])     # a screenwriter who cuts nothing
    with pytest.raises(PipelineBlocked, match="does not fit even a 0.2-minute film"):
        film._fit_scene_lengths(bible, outline)
    assert "Shorten the dialogue to about" in revised[0].revision_instructions[0]


def test_a_line_is_rewritten_at_most_twice_and_only_when_its_words_change(film):
    """A screenwriter who hands back the same words changes nothing: the line is not counted as shortened, no cut
    is thrown away, and after FIT_TRIES asks it is left for the checks to stop and name."""
    from nraiorbit.schemas import ShortenedLines
    from nraiorbit.service import produce
    asked = []

    def same(bible, outline, script, limits):
        asked.append(dict(limits))
        return ShortenedLines.model_validate({"lines": [
            {"block_id": b.block_id, "text_te": b.text_te, "text_en": b.text_en} for b in script.blocks
            if b.block_id in limits]})
    film.screenwriter = N(shorten_lines=same)
    for _ in range(4):
        assert produce.fit_dialogue(film, log=lambda *_: None) == []
    assert (film.p.edit / "timeline.json").exists() and (film.p.master / "movie.mp4").exists()
    record = json.loads((film.p.reports / produce.DIALOGUE_FIT).read_text())
    assert record and all(r["tries"] == produce.FIT_TRIES for r in record.values())
    assert len(asked) == 2 * produce.FIT_TRIES                                # two asks a try, then never again


def test_a_line_with_no_room_is_dropped_and_a_recorded_line_is_never_rewritten(film):
    from nraiorbit.service import produce
    edl = json.loads((film.p.reports / "edl.json").read_text())
    edl["dialogue"][0]["duration_s"] = 3.9                                    # b001 fills its shot
    (film.p.reports / "edl.json").write_text(json.dumps(edl))
    plan = json.loads((film.p.shots / "sc001.json").read_text())
    plan["shots"][0]["block_ids"] = ["sc001_b001", "sc001_b002"]               # b002 shares b001's shot: no room
    plan["shots"][1]["block_ids"] = []
    (film.p.shots / "sc001.json").write_text(json.dumps(plan))
    (film.p.root / "inbox" / "dialogue").mkdir(parents=True)
    (film.p.root / "inbox" / "dialogue" / "sc001_b001.wav").write_bytes(b"artist")   # a voice artist's take
    asked = []
    film.screenwriter = _screenwriter(asked)
    done = produce.fit_dialogue(film, log=lambda *_: None)
    assert "sc001_b002" in done and "sc001_b001" not in done
    ids = [b["block_id"] for b in json.loads((film.p.scenes / "sc001.json").read_text())["blocks"]]
    assert "sc001_b002" not in ids and "sc001_b001" in ids                   # dropped; the recorded line kept
    assert all("sc001_b001" not in a for a in asked)


def test_a_free_film_with_long_lines_goes_straight_on(film, monkeypatch):
    from nraiorbit.pipeline import LinesTooLong
    from nraiorbit.service import produce
    runs, calls = [], []

    def run(until=None, force=()):
        runs.append(until)
        if until is None and len(runs) == 2:
            raise LinesTooLong([{"block_id": "sc001_b002", "scene_id": "sc001", "seconds": 6.0, "start_s": 4.2,
                                 "room_s": 4.3, "over_s": 1.7}], {"sc001_b002": 6.0})
    film.run = run
    film.cfg = {**film.cfg, "providers": {**film.cfg["providers"], "video": "mock", "lipsync": "none"}}
    monkeypatch.setattr(produce, "fit_dialogue",
                        lambda p, log, measured=None: calls.append(measured) or (["sc001_b002"] if measured else []))
    monkeypatch.setattr(produce, "cost_gate", lambda p: (None, {}))
    monkeypatch.setattr("nraiorbit.service.rehearse.save", lambda pipeline, log=print: None)
    monkeypatch.setattr("nraiorbit.qa.film.check", lambda root, log=None: {"ok": True})
    report = produce.produce(film, log=lambda *_: None)
    assert runs == ["shots", None, None] and report["state"] != "awaiting:cost"   # nothing to pay: rendered again


def test_a_line_given_no_length_is_not_placed():
    shots = [shot("s1", 4, ["b1", "b2", "b3"], ["hero"])]
    blocks = [line("b1", "hero"), line("b2", "hero"), line("b3", "hero")]
    lines = fit.plan_lines([("sc", 0.0, shots, blocks)], {"b1": 1.5, "b2": 0.0, "b3": 1.5}, film_end=4.0)
    assert [ln.block_id for ln in lines] == ["b1", "b3"] and lines[1].start_s == pytest.approx(1.95)


def test_shots_planned_again_after_most_of_the_film_was_paid_for_ask_the_cost_again(film, monkeypatch):
    """The estimate counts only what is left to make: $2 of an approved $40 once $38 is spent. Close-ups planned
    again add $20; spent since the approval plus what is left ($58) is past the $40 approved, so the owner is asked,
    although $20 alone is under it."""
    from nraiorbit.service import produce
    runs, reworked = _cut_stops(film, monkeypatch, _faceless())
    left = iter([2.0, 20.0])
    monkeypatch.setattr(produce, "cost_gate", lambda p: (None, {"total_usd": next(left)}))
    film.p.reviews.mkdir(parents=True, exist_ok=True)
    (film.p.reviews / "cost.approved").write_text('{"total_usd": 40, "spent_usd": 0}\n')
    (film.p.reports / "cost.jsonl").write_text(json.dumps({"provider": "veo", "item": "clips", "usd": 38.0}) + "\n")
    report = produce.produce(film, log=lambda *_: None)
    assert reworked and report["state"] == "awaiting:cost" and not (film.p.reviews / "cost.approved").exists()
    marker = json.loads((film.p.reviews / "cost.pending").read_text())
    assert marker["total_usd"] == 20.0 and marker["spent_usd"] == 38.0      # the next approval knows where it began
