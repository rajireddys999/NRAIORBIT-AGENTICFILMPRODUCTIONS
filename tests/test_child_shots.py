"""A shot planned before the keep-children-out rule (or a slip of the director) still shows a child, which Google's
video model refuses for this project: before anything is paid or sent, the director plans it again under the rule,
it moves to a fresh take (a new first frame, so the faces check asks again), and one the director cannot plan without
a child after two asks stops the run with the reason, once, instead of going to Google."""

import json
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit.characters import child_words, children_refused, cradled_infant
from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.demo import OUTLINE, SHOTS, STORY_BIBLE
from nraiorbit.pipeline import CHILD_STOPS, Pipeline
from nraiorbit.schemas import Outline, ReworkedShot, ShotList, StoryBible
from nraiorbit.service import produce

VEO = {"providers": {"video": "veo"}, "google": {"person_generation": "allow_adult"}}
FIRST, SECOND, THIRD = (s["shot_id"] for s in SHOTS["sc001"][:3])


def _bible():
    """The demo cast with an 8-year-old (the second character) and a monkey."""
    b = StoryBible.model_validate(STORY_BIBLE)
    kid = b.characters[1]
    kid = kid.model_copy(update={"physical": kid.physical.model_copy(update={"age": 8})})
    monkey = b.characters[2].model_copy(update={"id": "chintu", "name_en": "Chintu", "silent_animal": True,
                                                "physical": b.characters[2].physical.model_copy(update={"age": 5})})
    return b.model_copy(update={"characters": [b.characters[0], kid, monkey]})


@pytest.fixture
def film(tmp_path):
    root = tmp_path / "rain"
    root.mkdir()
    bible = _bible()
    (root / "01_story_bible.json").write_text(bible.model_dump_json(), encoding="utf-8")
    (root / "02_outline.json").write_text(Outline.model_validate(OUTLINE).model_dump_json(), encoding="utf-8")
    (root / "cast.lock.yaml").write_text(yaml.safe_dump({"lock_version": 1, "characters": {
        c.id: {"character_asset": f"{c.id}_x", "voice_asset": None, "costumes": {}} for c in bible.characters}}))
    plan = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]})
    kid, hero = bible.characters[1].id, bible.characters[0].id
    shots = [plan.shots[0].model_copy(update={"characters_in_frame": [kid, hero], "costume_ids": ["", ""]}),
             plan.shots[1].model_copy(update={"characters_in_frame": ["chintu", hero], "costume_ids": ["", ""],
                                              "image_prompt": "the hero cradles tiny baby chintu against his chest"}),
             plan.shots[2].model_copy(update={"characters_in_frame": [hero], "costume_ids": [""]})]
    (root / "04_shots").mkdir()
    (root / "04_shots" / "sc001.json").write_text(plan.model_copy(update={"shots": shots}).model_dump_json(),
                                                 encoding="utf-8")
    for sid in (FIRST, SECOND):                                    # refused once: a first frame, no clip
        (root / "06_keyframes" / sid).mkdir(parents=True)
        (root / "06_keyframes" / sid / "take_1.png").write_bytes(b"png")
    p = Pipeline(root, None, cfg=deep_merge(load_pipeline_config(), VEO), log=lambda *_: None)
    return p, bible


def _director(bible, keeps_child=()):
    """A director that plans the shot again without the child, except for the shots in keeps_child."""
    asked = []
    kid = bible.characters[1].id

    def rework_shot(bible_, lock, shot, notes, off_screen=None, reason=""):
        asked.append((shot.shot_id, notes, off_screen, reason))
        if shot.shot_id in keeps_child:
            return ReworkedShot(characters_in_frame=[kid], costume_ids=[""], image_prompt=f"{kid} on the wall",
                                motion_prompt="she waves")
        return ReworkedShot(characters_in_frame=[bible.characters[0].id], costume_ids=[""],
                            image_prompt="the hero looks toward the courtyard, smiling at a voice",
                            motion_prompt="he turns and smiles")
    return SimpleNamespace(rework_shot=rework_shot), asked


def test_the_checks_read_the_words_and_the_project():
    assert cradled_infant("He cradles tiny baby Chintu against his chest")
    assert cradled_infant("Narasimha gently rocks the shivering baby")
    assert cradled_infant("offering a small steel spoon of warm milk to the sleepy baby chintu")
    assert not cradled_infant("a baby macaque sits on the wall")            # a baby, not held like an infant
    for words in ("a baby goat leaps across the rocks", "the baby elephant feeds by the river", "he nurses his arm",
                  "the hero rocks the boat", "a baby-faced hero smiles", "she cradles a pot; far across the "
                  "valley, many miles away from the village, a baby goat bleats on a distant hill"):
        assert not cradled_infant(words), words
    assert child_words("village kids chase a kite past the schoolgirl") == ["kids", "schoolgirl"]
    assert child_words("the hero and the heroine walk to the temple") == []
    for words in ("a goat kid bleats", "the hero, a kid at heart", "a Children's Day banner", "her child-like grin",
                  "the young girl he loved, now 25, waits"):
        assert child_words(words) == [], words
    assert children_refused(VEO)
    assert not children_refused({**VEO, "google": {"person_generation": "allow_all"}})
    assert not children_refused({"providers": {"video": "mock"}})


def _third(p, **update):
    plan = ShotList.model_validate_json((p.p.shots / "sc001.json").read_text())
    third = plan.shots[2].model_copy(update=update)
    (p.p.shots / "sc001.json").write_text(plan.model_copy(update={"shots": [*plan.shots[:2], third]})
                                          .model_dump_json())


def test_shots_the_video_step_would_still_send_that_show_a_child_are_found(film):
    p, bible = film
    kid = bible.characters[1]
    assert p.shots_showing_children() == {
        FIRST: ("shows", f"it shows {kid.name_en} (8)", f"{kid.name_en} (8), id {kid.id}"),
        SECOND: ("infant", "it calls Chintu a baby held like an infant", "Chintu")}
    inbox = p.p.root / "inbox" / "video"
    (inbox / ".pending").mkdir(parents=True)
    (inbox / f"{FIRST}.mp4").write_bytes(b"clip")                          # its clip is made: left alone
    (inbox / ".pending" / f"{SECOND}.json").write_text("{}")               # a paid job waits: left alone
    assert p.shots_showing_children() == {}
    (inbox / f"{FIRST}.mp4").unlink()
    (inbox / f"{FIRST}_2.mp4").write_bytes(b"clip")                        # take 2's own clip, numbers with a gap
    p.takes[FIRST] = 2
    assert FIRST not in p.shots_showing_children()
    _third(p, motion_prompt=f"the hero turns as {kid.name_en} runs in")
    assert p.shots_showing_children()[THIRD][:2] == ("names", f"it names {kid.name_en} (8)")   # named, not in frame
    _third(p, image_prompt="village kids chase a kite past the hero", motion_prompt="the kite dips")
    assert p.shots_showing_children()[THIRD] == ("words", "it describes a child (kids)", "")
    _third(p, image_prompt="the hero, a kid at heart, under a Children's Day banner", motion_prompt="he laughs")
    assert THIRD not in p.shots_showing_children()
    p.cfg["render"] = {**(p.cfg.get("render") or {}), "only": ["sc002"]}   # a preview of another scene
    assert p.shots_showing_children() == {}
    p.cfg["render"]["only"] = []
    p.cfg["google"]["person_generation"] = "allow_all"
    assert p.shots_showing_children() == {}


def test_they_are_planned_again_without_the_child_before_anything_is_paid(film):
    p, bible = film
    p.director, asked = _director(bible)
    redone, stop = produce.replan_child_shots(p, log=lambda *_: None)
    assert redone == [FIRST, SECOND] and stop == ""
    kid = bible.characters[1]
    first_note = asked[0][1]
    assert asked[0][0] == FIRST and f"{kid.name_en} (8), id {kid.id} must not be in this shot" in first_note
    assert kid.id in asked[0][2]                                            # and the rule that keeps them out
    assert "never their id or name" in first_note and "holds or leaves behind" not in first_note
    assert asked[0][3].startswith("Google's video model refuses to show a child")   # not "the owner sent it back"
    assert asked[1][0] == SECOND and "Draw Chintu as the young animal" in asked[1][1]
    assert p.shots_showing_children() == {}
    assert p.takes == {FIRST: 2, SECOND: 2}         # a fresh take: a new first frame, which the faces check shows
    assert produce.replan_child_shots(p, log=lambda *_: None) == ([], "") and len(asked) == 2


def test_a_shot_still_showing_a_child_after_two_asks_stops_the_run_once(film):
    p, bible = film
    p.director, asked = _director(bible, keeps_child={FIRST})
    redone, stop = produce.replan_child_shots(p, log=lambda *_: None)
    assert redone == [SECOND] and [a[0] for a in asked] == [FIRST, SECOND, FIRST]
    assert asked[2][1].startswith(f"Your last plan for this shot still shows {bible.characters[1].name_en} (8). ")
    assert stop.startswith(f"1 shot(s) still show a child after the director planned them again: {FIRST} (it shows ")
    assert "Send them back with a note" in stop
    assert set(json.loads((p.p.reports / CHILD_STOPS).read_text())) == {FIRST}
    assert p.takes[FIRST] == 2                       # asked twice, one fresh take: no gap in the take numbers
    again = produce.replan_child_shots(p, log=lambda *_: None)             # the next run: no third ask
    assert again == ([], stop) and len(asked) == 3
    plan = ShotList.model_validate_json((p.p.shots / "sc001.json").read_text())   # the owner sends it back:
    changed = plan.shots[0].model_copy(update={"image_prompt": "a new plan that still shows the child"})
    (p.p.shots / "sc001.json").write_text(plan.model_copy(update={"shots": [changed, *plan.shots[1:]]})
                                          .model_dump_json())
    produce.replan_child_shots(p, log=lambda *_: None)                      # a new plan is asked about again
    assert len(asked) == 5


def test_the_run_stops_before_the_cost_check(film, monkeypatch):
    p, _ = film
    p.run = lambda until=None, force=(): None
    monkeypatch.setattr(produce, "replan_child_shots", lambda pipeline, log: ([], "1 shot(s) still show a child"))
    monkeypatch.setattr(produce, "cost_gate", lambda pipeline: pytest.fail("the cost check came first"))
    monkeypatch.setattr("nraiorbit.service.rehearse.save", lambda pipeline, log=print: None)
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "blocked:1 shot(s) still show a child"
    assert report["steps"]["children"] == "blocked: 1 shot(s) still show a child"


def test_a_shot_that_only_names_the_child_goes_on_after_two_asks(film):
    """Naming her is not what Google refuses outright: the run is never stopped for it."""
    p, bible = film
    kid = bible.characters[1]
    hero = bible.characters[0].id
    asked = []

    def rework_shot(bible_, lock, shot, notes, off_screen=None, reason=""):
        asked.append(shot.shot_id)
        return ReworkedShot(characters_in_frame=[hero], costume_ids=[""], image_prompt="the hero by the wall",
                            motion_prompt=f"he smiles as {kid.name_en} calls from inside")
    p.director = SimpleNamespace(rework_shot=rework_shot)
    redone, stop = produce.replan_child_shots(p, log=lambda *_: None)
    assert stop == "" and redone == [FIRST, SECOND] and asked == [FIRST, SECOND, FIRST, SECOND]
    left = json.loads((p.p.reports / CHILD_STOPS).read_text())
    assert set(left) == {FIRST, SECOND} and all(e["goes_on"] for e in left.values())
    assert produce.replan_child_shots(p, log=lambda *_: None) == ([], "") and len(asked) == 4   # not asked again


def test_a_stops_file_that_is_not_a_record_is_read_as_none(film):
    p, bible = film
    p.director, _ = _director(bible)
    (p.p.reports).mkdir(parents=True, exist_ok=True)
    (p.p.reports / CHILD_STOPS).write_text("[1, 2]")
    assert produce.replan_child_shots(p, log=lambda *_: None)[1] == ""


def test_a_child_who_shares_a_name_with_an_adult_is_found_by_id_only(film):
    """A hero and his younger self: the hero's own shots name him, which is not the child."""
    p, bible = film
    kid, hero = bible.characters[1], bible.characters[0]
    young = kid.model_copy(update={"name_en": hero.name_en})
    bible = bible.model_copy(update={"characters": [hero, young, bible.characters[2]]})
    p.p.bible.write_text(bible.model_dump_json(), encoding="utf-8")
    _third(p, image_prompt=f"{hero.name_en} walks to the temple", motion_prompt="he climbs the steps")
    assert THIRD not in p.shots_showing_children()
    _third(p, image_prompt=f"{kid.id} waits at the temple gate", motion_prompt="the bell rings")
    assert p.shots_showing_children()[THIRD][0] == "names"


def test_a_baby_with_an_adult_animal_in_frame_is_not_blamed_on_the_animal(film):
    p, bible = film
    _third(p, characters_in_frame=["chintu", bible.characters[0].id], costume_ids=["", ""],
           image_prompt="the hero cradles a newborn baby in his arms by the old stone well in the courtyard, while "
                        "far behind them on the wall chintu watches", motion_prompt="he hums")
    assert p.shots_showing_children()[THIRD] == ("infant", "it has a baby held like an infant", "")


def test_a_child_word_is_asked_about_once_and_records_outside_the_run_are_kept(film):
    p, bible = film
    asked = []

    def rework_shot(bible_, lock, shot, notes, off_screen=None, reason=""):
        asked.append(shot.shot_id)
        return ReworkedShot(characters_in_frame=[bible.characters[0].id], costume_ids=[""],
                            image_prompt="village kids cheer the hero", motion_prompt="they wave")
    p.director = SimpleNamespace(rework_shot=rework_shot)
    redone, stop = produce.replan_child_shots(p, log=lambda *_: None)
    assert stop == "" and asked == [FIRST, SECOND]                          # a child word: one ask, then it goes on
    assert produce.replan_child_shots(p, log=lambda *_: None) == ([], "") and len(asked) == 2
    p.cfg["render"] = {**(p.cfg.get("render") or {}), "only": ["sc002"]}   # a preview of another scene
    produce.replan_child_shots(p, log=lambda *_: None)
    assert set(json.loads((p.p.reports / CHILD_STOPS).read_text())) == {FIRST, SECOND}   # their records stay


def test_a_takes_own_clip_is_found_when_take_numbers_skip(tmp_path):
    from nraiorbit.shotpack import clip_for_take
    inbox = tmp_path / "inbox" / "video"
    inbox.mkdir(parents=True)
    (inbox / "sc001_s001_3.mp4").write_bytes(b"clip")
    assert clip_for_take(tmp_path, "sc001_s001", 3) == inbox / "sc001_s001_3.mp4"   # one file, at position 1
    assert clip_for_take(tmp_path, "sc001_s001", 2) is None
    assert clip_for_take(tmp_path, "sc001_s001", 1) == inbox / "sc001_s001_3.mp4"   # take 1 by position, as before
