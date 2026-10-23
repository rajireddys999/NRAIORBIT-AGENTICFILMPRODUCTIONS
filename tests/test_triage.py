"""The owner rejects the cut or the trailer with a note: the editor reads it against the film (every shot and line with
its time, who is in frame), decides the narrowest fix, and the decision is applied with the steps that exist (a new
mix, a trailer direction, remade shots, an earlier step). A note it cannot tie to the film changes nothing and asks;
an editor that cannot be reached leaves the film as it was and the request for the next run."""

import json
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit.config import load_pipeline_config
from nraiorbit.demo import OUTLINE, SCENES, SHOTS, STORY_BIBLE
from nraiorbit.llm import LLMError, ReplayLLM
from nraiorbit.pipeline import EDL, OWNER_MIX, OWNER_NOTES, TRAILER_DIRECTION, TRAILER_PLAN, Pipeline
from nraiorbit.planning.runtime import scene_start_times
from nraiorbit.post.trailer import PACE, SECTIONS, plan_trailer, unmet
from nraiorbit.schemas import Outline, ReworkedShot, SceneScript, ShotList, StoryBible, TrailerDirection, TriageAction
from nraiorbit.service import produce, triage

NO_MIX = {"music_gain_db": 0, "min_voice_over_music_db": 0}


def act(route, shots=(), notes="", **mix):
    return {"route": route, "shots": list(shots), "notes_for_agents": notes, "mix": {**NO_MIX, **mix}}


def decision(*actions, question="", code=False):
    return {"understood": "The music is too loud.", "actions": list(actions), "why": "A quieter score fixes it.",
            "question": question, "needs_code_change": code}


DIRECTION = {"must_shots": ["sc001_s002"], "avoid_shots": ["sc002_s003"], "must_lines": [], "avoid_lines": [],
             "punch_line": "", "target_seconds": 0, "pace": "same", "music_notes": "softer drums", "why": "Opens dark."}


@pytest.fixture
def film(tmp_path):
    """The demo film, cut: plan files, an EDL with every shot and line placed, a master, a trailer, and every
    approval up to the trailer review."""
    root = tmp_path / "rain"
    root.mkdir()
    bible = StoryBible.model_validate(STORY_BIBLE)
    outline = Outline.model_validate(OUTLINE)
    (root / "01_story_bible.json").write_text(bible.model_dump_json(), encoding="utf-8")
    (root / "02_outline.json").write_text(outline.model_dump_json(), encoding="utf-8")
    (root / "cast.lock.yaml").write_text(yaml.safe_dump({"lock_version": 1, "characters": {
        c.id: {"character_asset": f"{c.id}_x", "voice_asset": None, "costumes": {}} for c in bible.characters}}))
    (root / "03_scenes").mkdir()
    (root / "04_shots").mkdir()
    starts = scene_start_times(outline)
    edl = {"film_seconds": 180, "shots": [], "dialogue": []}
    for sc in outline.scenes:
        (root / "03_scenes" / f"{sc.scene_id}.json").write_text(SceneScript.model_validate(
            {"scene_id": sc.scene_id, "heading": sc.heading, "synopsis": sc.synopsis, "rasa_primary": sc.rasa_primary,
             "intensity": sc.intensity, "blocks": SCENES[sc.scene_id]}).model_dump_json(), encoding="utf-8")
        plan = ShotList.model_validate({"scene_id": sc.scene_id, "shots": SHOTS[sc.scene_id]})
        (root / "04_shots" / f"{sc.scene_id}.json").write_text(plan.model_dump_json(), encoding="utf-8")
        t = starts[sc.scene_id]
        for shot in plan.shots:
            edl["shots"].append({"shot_id": shot.shot_id, "scene_id": sc.scene_id, "start_s": t,
                                 "duration_s": shot.duration_s, "real": shot.shot_id != "sc004_s001"})
            edl["dialogue"] += [{"ref_id": b, "start_s": t + 0.2, "duration_s": 3.0} for b in shot.block_ids
                                if b in {"sc001_b002", "sc002_b002", "sc003_b002", "sc004_b002"}]
            t += shot.duration_s
    (root / "reports").mkdir()
    (root / "reports" / EDL).write_text(json.dumps(edl))
    (root / "inbox" / "video").mkdir(parents=True)
    for s in edl["shots"]:
        (root / "inbox" / "video" / f"{s['shot_id']}.mp4").write_bytes(b"clip")
    (root / "11_master").mkdir()
    (root / "11_master" / "movie.mp4").write_bytes(b"film")
    (root / "11_master" / "trailer.mp4").write_bytes(b"trailer")
    (root / "reviews").mkdir()
    for g in ("cost", "faces", "final_cut", "trailer"):
        (root / "reviews" / f"{g}.approved").write_text("ok")

    def make(fixtures):
        p = Pipeline(root, ReplayLLM(fixtures), cfg=load_pipeline_config(), log=lambda *_: None)
        p.remove_remote = lambda paths: None
        return p
    return root, make


def reject(root, notes="the music drowns the lines", gate="trailer", rid="t1", waiting=True):
    """The owner's Reject at `gate`, with the film waiting there (unless waiting=False)."""
    if waiting:
        (root / "reviews" / f"{gate}.pending").write_text("x")
    (root / produce.REWORK_FILE).write_text(json.dumps({"id": rid, "mode": "triage", "gate": gate, "notes": notes,
                                                        "by": "red@x", "at": "2026-10-23T10:00:00+00:00"}))


def last_note(root):
    return json.loads((root / "reports" / OWNER_NOTES).read_text(encoding="utf-8").splitlines()[-1])


def test_the_editor_reads_every_shot_and_line_with_its_time_and_who_is_in_frame(film):
    root, make = film
    p = make({})
    text = triage.context(p, "final_cut")
    assert "sc002_s001 0:25 8s WS veera [arjun]: " in text                 # scene 2 starts 25 s in
    assert "sc004_s001 1:30 10s insert raudra [nobody] NO VIDEO" in text   # a placeholder is named as one
    assert "sc002_b002 0:33 arjun: " in text and "arjun: Arjun, hero, age 32" in text
    assert "Money: $0.00 spent" in text and "no trailer exists yet" in text
    assert "Trailer as cut" not in text
    (root / "reports" / TRAILER_PLAN).write_text(json.dumps([
        {"section": "punch", "shot_id": "sc002_s002", "film_start_s": 36.0, "line_id": "sc002_b002"},
        {"section": "title", "shot_id": None, "film_start_s": 0.0}]))
    text = triage.context(p, "trailer")
    assert "punch: sc002_s002 at film 0:36, line sc002_b002" in text and "title: title card" in text


def test_without_a_cut_the_times_come_from_the_outline(film):
    root, make = film
    (root / "reports" / EDL).unlink()
    shots, lines = triage.film_map(make({}))
    at = {s["shot_id"]: s["start_s"] for s in shots}
    assert at["sc002_s001"] == 25 and at["sc002_s002"] == 33          # scene start + the shots before it
    assert {ln["line_id"]: ln["start_s"] for ln in lines}["sc002_b002"] == 33


def test_only_what_can_be_applied_is_kept():
    known = {"sc001_s001", "sc002_s001"}
    v = lambda *a, gate="trailer": triage.validate([TriageAction.model_validate(x) for x in a], known, gate)  # noqa: E731
    kept, why = v(act("reshoot_shots", ["sc999_s001", "sc002_s001", "sc002_s001"]))
    assert [a.shots for a in kept] == [["sc002_s001"]] and why == ""
    kept, why = v(act("trailer"), gate="final_cut")
    assert kept == [] and "cannot be changed at this review" in why
    kept, _ = v(act("mix", music_gain_db=-30, min_voice_over_music_db=3))
    assert kept[0].mix.music_gain_db == -12 and kept[0].mix.min_voice_over_music_db == 6
    kept, why = v(act("mix"))
    assert kept == [] and "changes nothing" in why
    kept, _ = v(act("reshoot_shots", ["sc002_s001"], "no child"), act("shots", notes="fewer close-ups"))
    assert [a.route for a in kept] == ["shots"] and kept[0].notes_for_agents == "fewer close-ups no child"
    kept, _ = v(act("trailer", notes="a"), act("trailer", notes="b"), act("mix", music_gain_db=-3), act("music"))
    assert [(a.route, a.notes_for_agents) for a in kept] == [("trailer", "a"), ("mix", ""), ("music", "")]
    kept, _ = v(act("trailer"), act("mix", music_gain_db=-3), act("music"), act("reshoot_shots", ["sc001_s001"]))
    assert [a.route for a in kept] == ["trailer", "mix", "music"]                      # 3 at most, once each
    kept, _ = v(act("trailer"), act("scenes", notes="x"))
    assert [a.route for a in kept] == ["scenes"]               # a new plan: the trailer's ids would name other shots
    many = [f"sc{i:03d}_s001" for i in range(1, 14)]
    kept, why = triage.validate([TriageAction.model_validate(act("reshoot_shots", many))], set(many), "trailer")
    assert kept == [] and "Shot lists" in why


def test_a_quieter_score_is_mixed_again_for_free(film):
    root, make = film
    p = make({"TriageDecision": decision(act("mix", music_gain_db=-6, min_voice_over_music_db=14))})
    reject(root)
    out = produce.apply_rework(p, log=lambda *_: None)
    assert out["mode"] == "triage" and out["stage"] == "assemble" and "music -6 dB" in out["what"]
    assert json.loads((root / "reports" / OWNER_MIX).read_text())["music_gain_db"] == -6
    assert p._owner_mix() == (-6.0, 14.0)
    assert not (root / "11_master" / "movie.mp4").exists() and not (root / "11_master" / "trailer.mp4").exists()
    assert not (root / "reviews" / "final_cut.approved").exists()          # the new mix is reviewed again
    assert (root / "reviews" / "cost.approved").exists() and (root / "reviews" / "faces.approved").exists()
    note = last_note(root)
    assert note["id"] == "t1" and note["owner_notes"] == "the music drowns the lines" and note["gate"] == "trailer"
    assert note["understood"] and note["why"] and note["actions"][0]["route"] == "mix" and not note["question"]
    reject(root, rid="t2")                                                  # a second one adds to the first
    produce.apply_rework(make({"TriageDecision": decision(act("mix", music_gain_db=-6))}), log=lambda *_: None)
    assert json.loads((root / "reports" / OWNER_MIX).read_text()) == {"rework_id": "t2", "music_gain_db": -12,
                                                                       "min_voice_over_music_db": 14.0}


def test_a_trailer_note_becomes_a_direction_and_only_the_trailer_is_cut_again(film):
    root, make = film
    p = make({"TriageDecision": decision(act("trailer", notes="open dark, skip the river walk")),
              "TrailerDirection": DIRECTION})
    reject(root, notes="the trailer opens on the wrong thing")
    out = produce.apply_rework(p, log=lambda *_: None)
    assert out["stage"] == "trailer" and "the trailer is cut again: Opens dark." in out["what"]
    saved = json.loads((root / "reports" / TRAILER_DIRECTION).read_text())
    assert saved["rework_id"] == "t1" and saved["notes"] == "open dark, skip the river walk"
    assert p._trailer_direction() == TrailerDirection.model_validate(DIRECTION)
    assert not (root / "11_master" / "trailer.mp4").exists() and (root / "11_master" / "movie.mp4").exists()
    assert (root / "reviews" / "final_cut.approved").exists() and not (root / "reviews" / "trailer.approved").exists()
    assert p.editor.llm.calls == [("editor", "TriageDecision"), ("editor", "TrailerDirection")]


def test_remade_shots_ask_the_cost_again(film):
    root, make = film
    p = make({"TriageDecision": decision(act("reshoot_shots", ["sc002_s001", "sc999_s001"], "the hero alone"))})
    asked = []

    def rework_shot(bible, lock, shot, notes, off_screen=None):
        asked.append((shot.shot_id, notes))
        return ReworkedShot(characters_in_frame=["arjun"], costume_ids=[""], image_prompt="arjun alone",
                            motion_prompt="he turns")
    p.director = SimpleNamespace(rework_shot=rework_shot)
    reject(root, gate="final_cut")
    out = produce.apply_rework(p, log=lambda *_: None)
    assert asked == [("sc002_s001", "the hero alone")] and out["shots"] == ["sc002_s001"]
    assert p.takes["sc002_s001"] == 2 and p.takes.get("sc002_s002", 1) == 1
    assert not (root / "reviews" / "cost.approved").exists()                # an AI-chosen spend is asked again
    assert "the cost check asks first" in out["what"]


def test_an_earlier_step_is_redone_with_fresh_takes_and_the_agents_follow_the_notes(film):
    root, make = film
    p = make({"TriageDecision": decision(act("scenes", notes="the villain needs a reason"))})
    reject(root)
    produce.apply_rework(p, log=lambda *_: None)
    assert not (root / "03_scenes").exists() and not (root / "04_shots").exists()
    assert p.takes["sc001_s001"] == 2                                         # no old clip lands on a new plan
    assert p.screenwriter.notes == "the villain needs a reason" and p.story_analyst.notes == ""


def test_a_note_it_cannot_tie_to_the_film_changes_nothing_and_asks(film):
    root, make = film
    p = make({"TriageDecision": decision(question="Which scene do you mean?")})
    reject(root, notes="make it better")
    out = produce.apply_rework(p, log=lambda *_: None)
    assert out["stage"] is None and out["question"] == "Which scene do you mean?"
    assert out["what"].startswith("nothing redone yet")
    assert (root / "11_master" / "trailer.mp4").exists() and (root / "reviews" / "trailer.approved").exists()
    assert not (root / produce.REWORK_FILE).exists()                         # answered once, by its id
    p = make({"TriageDecision": decision(act("trailer"))})                   # nothing it chose can be applied:
    reject(root, gate="final_cut", rid="t2")                                 # it says why and asks
    out = produce.apply_rework(p, log=lambda *_: None)
    assert "cannot be changed at this review" in out["question"] and triage.NO_ACTION in out["question"]
    assert (root / "11_master" / "movie.mp4").exists()


def test_an_editor_out_of_reach_leaves_the_film_and_the_request(film):
    root, make = film
    reject(root)
    with pytest.raises(LLMError):
        produce.apply_rework(make({}), log=lambda *_: None)
    assert (root / produce.REWORK_FILE).exists() and (root / "11_master" / "trailer.mp4").exists()
    assert not (root / "reports" / OWNER_NOTES).exists()
    p = make({"TriageDecision": decision(act("trailer"))})                   # the direction fails: still nothing undone
    with pytest.raises(LLMError):
        produce.apply_rework(p, log=lambda *_: None)
    assert (root / "11_master" / "trailer.mp4").exists() and (root / produce.REWORK_FILE).exists()


def test_a_reject_without_notes_is_dropped(film):
    root, make = film
    reject(root, notes="  ")
    assert produce.apply_rework(make({}), log=lambda *_: None) is None
    assert not (root / produce.REWORK_FILE).exists() and (root / "11_master" / "trailer.mp4").exists()


def test_sending_the_trailer_back_with_notes_gives_it_a_direction(film):
    root, make = film
    p = make({"TrailerDirection": DIRECTION})
    (root / produce.REWORK_FILE).write_text(json.dumps({"id": "s1", "stage": "trailer", "notes": "open dark"}))
    out = produce.apply_rework(p, log=lambda *_: None)
    assert out["stage"] == "trailer" and not (root / "11_master" / "trailer.mp4").exists()
    assert json.loads((root / "reports" / TRAILER_DIRECTION).read_text())["rework_id"] == "s1"


# ---------------------------------------------------------------- the trailer planner under a direction

def _cut():
    from test_trailer_publish import _film_lines, _film_shots
    shots, total = _film_shots()
    return shots, total, _film_lines(shots), StoryBible.model_validate(STORY_BIBLE)


def _shown(plan, shots, sid):
    shot, start = next((s, t) for s, t in shots if s.shot_id == sid)
    return any(c.shot_id and min(start + shot.duration_s, c.film_start_s + c.duration_s)
               - max(start, c.film_start_s) > 0.05 for c in plan)


def test_the_trailer_follows_the_owners_direction(film):
    shots, total, lines, bible = _cut()
    before = plan_trailer(shots, bible, total, 45, lines)
    assert _shown(before, shots, "sc002_s003") and not _shown(before, shots, "sc001_s002")
    assert next(c for c in before if c.section == "punch").line_id == "sc002_b002"
    d = TrailerDirection.model_validate({**DIRECTION, "must_lines": ["sc003_b002"], "punch_line": "sc001_b002",
                                         "avoid_lines": ["sc004_b002"]})
    plan = plan_trailer(shots, bible, total, 45, lines, direction=d)
    assert not _shown(plan, shots, "sc002_s003") and _shown(plan, shots, "sc001_s002")
    said = {c.line_id for c in plan if c.line_id}
    assert "sc003_b002" in said and "sc004_b002" not in said
    assert next(c for c in plan if c.section == "punch").line_id == "sc001_b002"
    assert unmet(plan, shots, total, d) == []
    assert sum(c.duration_s for c in plan) <= 45 + 0.5                      # still the length asked for


def test_the_pace_scales_the_cuts_and_the_ending_stays_out(film):
    shots, total, lines, bible = _cut()
    fast = TrailerDirection.model_validate({**DIRECTION, "must_shots": ["sc006_s003", "sc404_s001"],
                                            "avoid_shots": [], "pace": "faster"})
    plan = plan_trailer(shots, bible, total, 45, lines, direction=fast)
    longest = {name: max_clip for name, _, max_clip in SECTIONS}
    assert all(c.duration_s <= longest[c.section] * PACE["faster"] + 1e-6 for c in plan if c.shot_id and not c.line_id)
    assert not _shown(plan, shots, "sc006_s003")                             # the spoiler rule still holds
    assert unmet(plan, shots, total, fast) == [
        "shot sc006_s003 is not in the trailer: it is from the film's last minutes, which the trailer never shows",
        "shot sc404_s001 is not in the trailer: it has no finished video"]


def test_the_trailer_step_follows_the_direction_and_records_what_it_could_not_do(film, monkeypatch):
    """The pipeline hands the saved direction to the planner, keeps the plan in reports/ and lists what it could not
    honour; the owner's music note goes to the score."""
    root, make = film
    p = make({})
    (root / "reports" / TRAILER_DIRECTION).write_text(json.dumps(
        {"rework_id": "t1", "direction": {**DIRECTION, "must_shots": ["sc006_s003"], "target_seconds": 500}}))
    seen = {}

    def planner(shots, bible, film_seconds, target, lines, **kw):
        seen.update(target=target, direction=kw["direction"])
        return []
    monkeypatch.setattr("nraiorbit.pipeline.plan_trailer", planner)

    class Stop(Exception):
        pass

    def cut(*a, **kw):
        raise Stop
    monkeypatch.setattr("nraiorbit.pipeline.assemble.cut_segments", cut)
    (root / "10_edit").mkdir()
    edl = json.loads((root / "reports" / EDL).read_text())
    (root / "10_edit" / "timeline.json").write_text(json.dumps(
        {**edl, "dialogue": [{**e, "path": "x.wav"} for e in edl["dialogue"]], "music": []}))
    (root / "11_master" / "trailer.mp4").unlink()
    with pytest.raises(Stop):
        p._trailer()
    assert seen["target"] == 1.5 * p._film["trailer_seconds"] and seen["direction"].must_shots == ["sc006_s003"]
    assert json.loads((root / "reports" / TRAILER_PLAN).read_text()) == []
    saved = json.loads((root / "reports" / TRAILER_DIRECTION).read_text())
    assert saved["unmet"] == ["shot sc006_s003 is not in the trailer: it is from the film's last minutes, which "
                              "the trailer never shows"]


def test_the_cut_writes_where_every_shot_and_line_sits(film):
    root, make = film
    p = make({})
    (root / "reports" / EDL).unlink()
    (root / "10_edit").mkdir()
    (root / "10_edit" / "timeline.json").write_text(json.dumps({
        "film_seconds": 20, "shots": [{"shot_id": "sc001_s001", "scene_id": "sc001", "start_s": 0, "duration_s": 8,
                                       "clip": "a.mp4", "real": False}],
        "dialogue": [{"ref_id": "sc001_b002", "start_s": 1.2, "duration_s": 2.0, "path": "l.wav"}], "music": []}))
    p._write_edl()
    assert json.loads((root / "reports" / EDL).read_text()) == {
        "film_seconds": 20, "shots": [{"shot_id": "sc001_s001", "scene_id": "sc001", "start_s": 0, "duration_s": 8,
                                       "real": False}],
        "dialogue": [{"ref_id": "sc001_b002", "start_s": 1.2, "duration_s": 2.0}]}


def test_a_rejection_the_film_has_moved_on_from_is_recorded_and_never_applied(film):
    """Approved at that review since (or an older runner dropped it): applying it now would undo the approval."""
    root, make = film
    reject(root, gate="final_cut", waiting=False)
    out = produce.apply_rework(make({}), log=lambda *_: None)                # the editor is never asked
    assert out["skipped"] == "the film had moved on from the final cut review" and out["stage"] is None
    assert out["what"] == "not applied: the film had moved on from the final cut review"
    assert (root / "11_master" / "movie.mp4").exists() and (root / "reviews" / "final_cut.approved").exists()
    assert last_note(root)["id"] == "t1" and not (root / produce.REWORK_FILE).exists()
    (root / "reports" / "produce.json").write_text(json.dumps({"state": "done"}))   # a finished film: it applies
    reject(root, gate="done", rid="t2", waiting=False)
    out = produce.apply_rework(make({"TriageDecision": decision(act("mix", music_gain_db=-3))}), log=lambda *_: None)
    assert out["stage"] == "assemble" and not (root / "11_master" / "movie.mp4").exists()


def test_a_request_tried_again_changes_the_mix_once(film):
    root, make = film
    p = make({"TriageDecision": decision(act("mix", music_gain_db=-6))})
    req = {"id": "t1", "gate": "trailer", "notes": "quieter"}
    (root / "reviews" / "trailer.pending").write_text("x")
    triage.apply(p, req, log=lambda *_: None)
    (root / "reviews" / "trailer.pending").write_text("x")              # the run failed after it: tried again
    triage.apply(p, req, log=lambda *_: None)
    assert p._owner_mix() == (-6.0, 0.0)


def test_shots_planned_again_are_all_written_or_none(film):
    root, make = film
    p = make({"TriageDecision": decision(act("reshoot_shots", ["sc001_s001", "sc002_s001"], "x"))})
    before = {f.name: f.read_text() for f in (root / "04_shots").glob("*.json")}

    def rework_shot(bible, lock, shot, notes, off_screen=None):
        if shot.shot_id == "sc002_s001":
            raise LLMError("overloaded")
        return ReworkedShot(characters_in_frame=[], costume_ids=[], image_prompt="new", motion_prompt="new")
    p.director = SimpleNamespace(rework_shot=rework_shot)
    reject(root)
    with pytest.raises(LLMError):
        produce.apply_rework(p, log=lambda *_: None)
    assert {f.name: f.read_text() for f in (root / "04_shots").glob("*.json")} == before
    assert p.takes == {} and (root / "11_master" / "movie.mp4").exists() and (root / produce.REWORK_FILE).exists()


def test_a_new_shot_plan_clears_the_trailer_direction(film):
    root, make = film
    p = make({})
    (root / "reports" / TRAILER_DIRECTION).write_text(json.dumps({"rework_id": "t1", "direction": DIRECTION}))
    p.invalidate("trailer")
    assert p._trailer_direction() is not None                                 # a re-cut keeps it
    p.invalidate("shots")
    assert p._trailer_direction() is None
    assert json.loads((root / "reports" / TRAILER_DIRECTION).read_text())["direction"] is None


def test_the_editor_hears_the_sound_as_it_stands_and_can_only_raise_the_floor(film, monkeypatch):
    root, make = film
    p = make({})
    p.cfg["audio"]["min_voice_over_music_db"] = 12
    (root / "reports" / OWNER_MIX).write_text(json.dumps({"music_gain_db": -6, "min_voice_over_music_db": 9}))
    assert ("Sound: the music is -6 dB from the original mix; every line is kept at least 12 dB over the music."
            in triage.context(p, "final_cut"))
    seen = {}
    monkeypatch.setattr("nraiorbit.pipeline.clarify_dialogue",
                        lambda master, lines, wav, min_db, true_peak: seen.update(min_db=min_db) or [])
    p._clarify(root / "11_master" / "movie.mp4", [SimpleNamespace(path="x", start_s=0, gain_db=0)], "film")
    assert seen["min_db"] == 12                                               # the project's 12, not the owner's 9


def test_a_rejection_a_later_run_finished_without_applying_is_never_applied(film):
    """An older runner dropped it: a Retry days later must not undo what the owner approved since."""
    root, make = film
    (root / "reports" / "produce.json").write_text(json.dumps({"state": "awaiting:trailer",
                                                               "finished": "2026-10-23T11:00:00+00:00"}))
    reject(root)                                                               # asked at 10:00
    out = produce.apply_rework(make({}), log=lambda *_: None)
    assert out["skipped"] == "a run after it finished without applying it"
    assert (root / "11_master" / "trailer.mp4").exists()


def test_a_rejection_that_began_to_apply_is_carried_on(film):
    """The run stopped after the mix was changed (the gate's marker is gone): the retry finishes it, once."""
    root, make = film
    (root / "reports" / OWNER_MIX).write_text(json.dumps({"rework_id": "t1", "music_gain_db": -6}))
    reject(root, waiting=False)
    out = produce.apply_rework(make({"TriageDecision": decision(act("mix", music_gain_db=-6))}), log=lambda *_: None)
    assert not out.get("skipped") and p_mix(root) == -6


def p_mix(root):
    return json.loads((root / "reports" / OWNER_MIX).read_text())["music_gain_db"]


def test_a_trailer_direction_is_saved_only_once_the_shots_are_planned(film):
    root, make = film
    p = make({"TriageDecision": decision(act("reshoot_shots", ["sc002_s001"], "x"), act("trailer", notes="dark")),
              "TrailerDirection": DIRECTION})

    def rework_shot(*a, **k):
        raise LLMError("overloaded")
    p.director = SimpleNamespace(rework_shot=rework_shot)
    reject(root)
    with pytest.raises(LLMError):
        produce.apply_rework(p, log=lambda *_: None)
    assert not (root / "reports" / TRAILER_DIRECTION).exists()


def test_a_floor_below_the_one_in_force_changes_nothing(film):
    root, make = film
    p = make({"TriageDecision": decision(act("mix", min_voice_over_music_db=9))})
    p.cfg["audio"]["min_voice_over_music_db"] = 12
    reject(root)
    out = produce.apply_rework(p, log=lambda *_: None)
    assert out["stage"] is None and "changes nothing" in out["question"]
    assert (root / "11_master" / "movie.mp4").exists()
