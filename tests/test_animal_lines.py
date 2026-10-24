"""Animals make sounds, not speeches: a line the screenwriter gives an animal is kept as an action line when the scene
is saved, so no voice reads it and no lip-sync is paid on an animal's face; the screenwriter is told so up front; and
voice.animals_speak: true leaves a talking animal's lines alone."""

from types import SimpleNamespace

import pytest

from nraiorbit.agents.writers import Screenwriter
from nraiorbit.demo import OUTLINE, STORY_BIBLE
from nraiorbit.pipeline import Pipeline
from nraiorbit.schemas import Outline, SceneScript, ScriptBlock, StoryBible


@pytest.fixture
def bible():
    b = StoryBible.model_validate(STORY_BIBLE)
    monkey = b.characters[0]
    monkey = monkey.model_copy(update={"silent_animal": True, "physical": monkey.physical.model_copy(
        update={"build": "a small macaque with brown fur and a long tail"})})
    return b.model_copy(update={"characters": [monkey, *b.characters[1:]]})


def _block(n, kind, who, te, en):
    return ScriptBlock(block_id=f"sc001_b{n:03d}", kind=kind, character_id=who, text_te=te, text_en=en, delivery=None)


def _script(bible):
    monkey, person = bible.characters[0].id, bible.characters[1].id
    return SceneScript(scene_id="sc001", heading="h", synopsis="s",
                       rasa_primary=Outline.model_validate(OUTLINE).scenes[0].rasa_primary, intensity=5, blocks=[
                           _block(1, "dialogue", monkey, "కీ కీ!", "Screeches and points at the river"),
                           _block(2, "dialogue", person, "కాపాడండి!", "Help!"),
                           _block(3, "action", None, "The river rises.", "The river rises."),
                       ])


def test_an_animals_line_is_saved_as_its_sound_and_every_other_block_is_kept(bible):
    assert bible.characters[0].silent_animal and not bible.characters[1].silent_animal
    logs, script = [], _script(bible)
    out = Pipeline._animal_lines_as_sound(SimpleNamespace(cfg={}, log=logs.append), script, bible)
    sound = out.blocks[0]
    assert (sound.kind, sound.character_id, sound.delivery, sound.block_id) == ("action", None, None, "sc001_b001")
    name = bible.characters[0].name_en
    assert sound.text_en == sound.text_te == (f"{name} makes animal sounds and gestures, no words "
                                              "(meaning: Screeches and points at the river)")
    assert out.blocks[1:] == script.blocks[1:]                             # the person's line and the action stay
    assert logs == ["  sc001: 1 animal line(s) kept as sound, not speech"]


def test_talking_animals_and_stories_without_animals_are_left_alone(bible):
    script = _script(bible)
    talking = SimpleNamespace(cfg={"voice": {"animals_speak": True}}, log=lambda *_: None)
    assert Pipeline._animal_lines_as_sound(talking, script, bible) is script
    this_one = SimpleNamespace(cfg={"voice": {"animals_speak": [bible.characters[0].id]}}, log=lambda *_: None)
    assert Pipeline._animal_lines_as_sound(this_one, script, bible) is script     # the listed animal talks
    other = SimpleNamespace(cfg={"voice": {"animals_speak": ["someone_else"]}}, log=lambda *_: None)
    assert Pipeline._animal_lines_as_sound(other, script, bible).blocks[0].kind == "action"
    people = StoryBible.model_validate(STORY_BIBLE)
    plain = SceneScript.model_validate({**script.model_dump(), "blocks": [b.model_dump() for b in script.blocks[1:]]})
    assert Pipeline._animal_lines_as_sound(SimpleNamespace(cfg={}, log=lambda *_: None), plain, people) is plain


def test_the_screenwriter_is_told_which_characters_never_speak_when_writing_and_revising(bible):
    seen = []

    class Recorder:
        def generate(self, *, schema, system, context, task, effort, agent):
            seen.append(task)
            return _script(bible)
    outline = Outline.model_validate(OUTLINE)
    writer = Screenwriter(Recorder(), {})
    writer.scene(bible, outline, outline.scenes[0], None)
    from nraiorbit.schemas import Critique
    writer.revise(bible, outline, _script(bible), Critique(score=3, issues=["flat"], revision_instructions=["more"]))
    rule = f"These characters are animals and never speak: {bible.characters[0].name_en}."
    assert all(rule in task for task in seen) and len(seen) == 2
    Screenwriter(Recorder(), {"voice": {"animals_speak": True}}).scene(bible, outline, outline.scenes[0], None)
    Screenwriter(Recorder(), {}).scene(StoryBible.model_validate(STORY_BIBLE), outline, outline.scenes[0], None)
    assert "never speak" not in seen[2] and "never speak" not in seen[3]



def test_only_a_character_the_bible_marks_as_an_animal_loses_its_lines(bible):
    """Words never decide it: a person written with "a lion's build" or "grey whiskers" keeps every line; so does an
    animal an older bible did not mark (its lines then stop the film at the voices check, as before)."""
    from nraiorbit.characters import silent_animals, talking_animals
    person = bible.characters[1]
    lion = person.model_copy(update={"physical": person.physical.model_copy(
        update={"build": "a lion's build, a bull's neck", "face": "bushy white whiskers", "hair": "a wild grey mane"})})
    unmarked = bible.characters[0].model_copy(update={"silent_animal": False})
    script = _script(bible)
    for b in (bible.model_copy(update={"characters": [unmarked, lion, *bible.characters[2:]]}),):
        out = Pipeline._animal_lines_as_sound(SimpleNamespace(cfg={}, log=lambda *_: None), script, b)
        assert out is script                                                   # nobody marked: nothing changes
    assert silent_animals(bible.characters, {}) == {bible.characters[0].id: bible.characters[0].name_en}
    for value, talk in [(True, True), ("true", True), ("Yes", True), ("all", True), (False, set()), (None, set()),
                        ("no", set()), ("chintu", {"chintu"}), (["chintu", "kothi"], {"chintu", "kothi"})]:
        assert talking_animals({"voice": {"animals_speak": value}}) == talk, value
    assert silent_animals(bible.characters, {"voice": {"animals_speak": bible.characters[0].id}}) == {}
    from nraiorbit.identity import is_animal
    assert is_animal(bible.characters[0]) and not is_animal(person)            # pictures follow the mark too

def test_the_scenes_step_saves_an_animals_line_as_its_sound(tmp_path, bible):
    """Through Pipeline._scenes itself: the script the screenwriter returns is saved with the animal's line as an
    action line, and the person's line is still dialogue (so the voices, lip-sync and costing see no animal speech)."""
    from nraiorbit.pipeline import ProjectPaths
    from nraiorbit.schemas import Critique
    paths = ProjectPaths(tmp_path)
    outline = Outline.model_validate(OUTLINE)
    one = Outline(scenes=[outline.scenes[0].model_copy(update={"scene_id": "sc001"})])
    paths.outline.parent.mkdir(parents=True, exist_ok=True)
    paths.outline.write_text(one.model_dump_json(), encoding="utf-8")
    paths.bible.write_text(bible.model_dump_json(), encoding="utf-8")
    logs = []
    p = SimpleNamespace(p=paths, cfg={"llm": {"critique_rounds": 1, "critique_pass_score": 8}}, log=logs.append,
                        screenwriter=SimpleNamespace(scene=lambda *a: _script(bible), revise=lambda *a: _script(bible)),
                        critic=SimpleNamespace(review=lambda *a: Critique(score=9, issues=[], revision_instructions=[])),
                        _report=lambda *a: None, _save=Pipeline._save, _load=Pipeline._load,
                        _script_problems=Pipeline._script_problems,
                        _fit_scene_lengths=lambda *a: None)              # the dialogue-fit check has its own tests
    p._animal_lines_as_sound = lambda script, b: Pipeline._animal_lines_as_sound(p, script, b)
    Pipeline._scenes(p)
    saved = SceneScript.model_validate_json((paths.scenes / "sc001.json").read_text(encoding="utf-8"))
    assert [(b.kind, b.character_id) for b in saved.blocks] == [("action", None),
                                                                ("dialogue", bible.characters[1].id),
                                                                ("action", None)]
    assert "1 animal line(s) kept as sound, not speech" in " ".join(logs)
