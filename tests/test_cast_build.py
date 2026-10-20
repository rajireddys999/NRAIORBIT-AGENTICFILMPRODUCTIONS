"""The cast step of a runner film builds what casting could not find: a library entry per new character, written
from the story bible, and reference pictures for every cast character still without them, before the cast gate."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit.demo import STORY_BIBLE
from nraiorbit.identity import character_manifest, is_animal, portrait_prompt, register_new_characters
from nraiorbit.library.registry import AssetRegistry
from nraiorbit.pipeline import Pipeline, PipelineBlocked, ProjectPaths
from nraiorbit.providers.google_ai import ShotFiltered
from nraiorbit.schemas import CastEntry, CastLock, CharacterAsset, StoryBible


def bible_with_a_monkey() -> StoryBible:
    bible = StoryBible.model_validate(STORY_BIBLE)
    hero, heroine, villain = bible.characters
    monkey = hero.model_copy(update={"id": "chintu", "name_en": "Chintu", "physical": hero.physical.model_copy(update={
        "age": 5, "build": "lean, agile and wiry, with a long expressive tail", "skin_tone": "golden-brown fur",
        "face": "bright amber eyes, a pink macaque face", "hair": "tufted crown"}), "costume_baseline": "no clothing"})
    return bible.model_copy(update={"characters": [monkey, heroine, villain]})


def library_with(tmp_path: Path, *ids: str) -> AssetRegistry:
    """A library holding characters under these ids (someone else's entries: the new cast must never take them)."""
    for aid in ids:
        folder = tmp_path / "library" / "characters" / aid
        folder.mkdir(parents=True)
        (folder / "manifest.yaml").write_text(yaml.safe_dump({"id": aid, "kind": "character", "version": 1,
                                                              "license": {"type": "owned", "commercial_use": True}}))
    (tmp_path / "library" / "characters").mkdir(parents=True, exist_ok=True)
    return AssetRegistry.load(tmp_path / "library")


def test_animals_are_told_apart_from_people_and_photographed_as_animals():
    bible = bible_with_a_monkey()
    monkey, heroine, _ = bible.characters
    assert is_animal(monkey) and not is_animal(heroine)
    ponytail = heroine.model_copy(update={"physical": heroine.physical.model_copy(update={
        "hair": "long black ponytail", "face": "a furrowed brow"})})
    assert not is_animal(ponytail)                               # "ponytail" and "furrowed" are a person's
    prompt = portrait_prompt(monkey, bible.era_and_region)
    assert "wildlife photograph" in prompt and "Telugu" not in prompt and "animal" in prompt
    assert "Telugu" in portrait_prompt(heroine, bible.era_and_region)


def test_new_characters_get_entries_named_for_their_film_and_the_lock_points_at_them(tmp_path):
    bible = bible_with_a_monkey()
    library = library_with(tmp_path, "meera")                    # an older film's Meera: not this one
    lock = CastLock(characters={"chintu": CastEntry(character_asset="characters/chintu@pending"),
                                "meera": CastEntry(character_asset="characters/meera@pending"),
                                "bhairava": CastEntry(character_asset="characters/meera@v1")})
    lock2, library2, added = register_new_characters(bible, lock, library, film="monkey-saves-man-2",
                                                     log=lambda *_: None)
    assert added == ["chintu_monkey_saves_man_2", "meera_monkey_saves_man_2"]   # another film's lakshmi never clashes
    assert lock2.characters["chintu"].character_asset == "characters/chintu_monkey_saves_man_2@v1"
    assert lock2.characters["meera"].character_asset == "characters/meera_monkey_saves_man_2@v1"
    assert lock2.characters["bhairava"] == lock.characters["bhairava"]              # cast from the library: kept
    asset = library2.get("character", "chintu_monkey_saves_man_2")
    assert isinstance(asset, CharacterAsset) and asset.usable_for_production()[0] and "animal" in asset.tags
    assert asset.physical["build"].endswith("expressive tail") and asset.costumes[0].description == "no clothing"
    assert lock2.characters["chintu"].costumes == {"cst_chintu_monkey_saves_man_2_main": "no clothing"}
    assert register_new_characters(bible, lock2, library2, film="monkey-saves-man-2",
                                   log=lambda *_: None)[2] == []                     # nothing pending: no-op
    # a run interrupted after the entries were written but before the lock: the same entries are taken up again
    manifest = tmp_path / "library" / "characters" / "chintu_monkey_saves_man_2" / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace("version: 1", "version: 1\n# kept"))
    lock3, _, again = register_new_characters(bible, lock, library2, film="monkey-saves-man-2", log=lambda *_: None)
    assert again == added and lock3 == lock2 and "# kept" in manifest.read_text()
    assert character_manifest(bible.characters[1], "meera_x")["tags"] == ["20s", "female", "heroine", "human"]


class FakeImages:
    """Writes a picture per request and records the prompts; refuses any prompt naming a word it is given."""

    def __init__(self, refuse: str | None = None):
        self.prompts, self.refuse = [], refuse

    def generate(self, prompt, out, **kw):
        if self.refuse and self.refuse in prompt:
            raise ShotFiltered("refused")
        self.prompts.append(prompt)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"png")


def cast_step(tmp_path, images, *library_ids):
    """Pipeline._build_cast over a project whose casting left Chintu and Meera pending and cast Bhairava."""
    bible = bible_with_a_monkey()
    paths = ProjectPaths(tmp_path / "film")
    paths.bible.parent.mkdir(parents=True, exist_ok=True)
    paths.bible.write_text(bible.model_dump_json(), encoding="utf-8")
    library = library_with(tmp_path, *library_ids)
    lock = CastLock(characters={"chintu": CastEntry(character_asset="characters/chintu@pending"),
                                "meera": CastEntry(character_asset="characters/meera@pending"),
                                "bhairava": CastEntry(character_asset="characters/bhairava_old@v1")})
    paths.lock.write_text(yaml.safe_dump(lock.model_dump()), encoding="utf-8")
    p = SimpleNamespace(p=paths, library=library, log=lambda *_: None, _identity_images=lambda: images,
                        _lock=lambda: CastLock.model_validate(yaml.safe_load(paths.lock.read_text())),
                        _load=lambda path, model: model.model_validate_json(Path(path).read_text()))
    return p, paths


def test_the_cast_step_builds_new_characters_and_pictures_once(tmp_path):
    images = FakeImages()
    p, paths = cast_step(tmp_path, images, "bhairava_old")
    Pipeline._build_cast(p)
    lock = CastLock.model_validate(yaml.safe_load(paths.lock.read_text()))
    assert not [c for c, e in lock.characters.items() if e.character_asset.endswith("@pending")]
    for aid in ("chintu_film", "meera_film", "bhairava_old"):      # the library one without pictures gets them too
        assert (tmp_path / "library" / "characters" / aid / "refs" / "face" / "01_front.png").exists()
    chintu = [t for t in images.prompts if "wildlife photograph" in t]
    assert chintu and all("same animal" in t or "head-and-shoulders" in t for t in chintu)
    made = len(images.prompts)
    Pipeline._build_cast(p)                                        # a second pass pays for nothing
    assert len(images.prompts) == made


def test_a_refused_portrait_blocks_with_the_characters_name(tmp_path):
    p, _ = cast_step(tmp_path, FakeImages(refuse="macaque"), "bhairava_old")
    with pytest.raises(PipelineBlocked, match=r"\['chintu'\] have no portrait"):
        Pipeline._build_cast(p)


def test_the_cast_step_runs_before_the_cast_gate_only_for_films_that_make_pictures(tmp_path):
    lock_file = tmp_path / "cast.lock.yaml"
    lock_file.write_text("characters: {}\n")
    calls = []
    for providers, build, expected in (({"video": "veo"}, True, ["build", "gate"]),
                                       ({"keyframe": "gemini"}, True, ["build", "gate"]),
                                       ({"video": "veo"}, False, ["gate"]),           # hand-made: identity build
                                       ({"video": "mock", "music": "lyria"}, True, ["gate"])):   # no pictures made
        calls.clear()
        p = SimpleNamespace(p=SimpleNamespace(lock=lock_file), cfg={"providers": providers,
                                                                     "cast": {"build_identity": build}},
                            _animatic=lambda: False, _build_cast=lambda: calls.append("build"),
                            gate=lambda g: calls.append("gate"), _lock=lambda: CastLock(characters={}))
        Pipeline._cast(p)
        assert calls == expected, providers



def voice_film(tmp_path, providers, voice):
    """A project with the demo's scenes and shot lists written and its speakers cast, for the voices check."""
    from nraiorbit.demo import OUTLINE, SCENES, SHOTS
    from nraiorbit.schemas import Outline, SceneScript, ShotList
    paths = ProjectPaths(tmp_path / "film")
    paths.scenes.mkdir(parents=True)
    paths.shots.mkdir(parents=True)
    for sc in Outline.model_validate(OUTLINE).scenes:
        if sc.scene_id in SCENES:
            script = SceneScript.model_validate({"scene_id": sc.scene_id, "heading": sc.heading, "synopsis": sc.synopsis,
                                                 "rasa_primary": sc.rasa_primary, "intensity": sc.intensity,
                                                 "blocks": SCENES[sc.scene_id]})
            (paths.scenes / f"{sc.scene_id}.json").write_text(script.model_dump_json(), encoding="utf-8")
        if sc.scene_id in SHOTS:
            sl = ShotList.model_validate({"scene_id": sc.scene_id, "shots": SHOTS[sc.scene_id]})
            (paths.shots / f"{sc.scene_id}.json").write_text(sl.model_dump_json(), encoding="utf-8")
    speakers = sorted({b["character_id"] for blocks in SCENES.values() for b in blocks
                       if b["kind"] == "dialogue" and b.get("character_id")})
    lock = CastLock(characters={c: CastEntry(character_asset=f"characters/{c}@v1") for c in speakers})
    p = SimpleNamespace(p=paths, cfg={"providers": providers, "voice": voice}, _lock=lambda: lock,
                        library=AssetRegistry.load(tmp_path / "empty-library"))
    carried = {b for blocks in SHOTS.values() for sh in blocks for b in sh.get("block_ids", [])}
    speakers = sorted({b["character_id"] for blocks in SCENES.values() for b in blocks
                       if b["kind"] == "dialogue" and b.get("character_id") and b["block_id"] in carried})
    return p, speakers


def test_a_speaking_character_without_an_elevenlabs_voice_stops_the_run_before_video(tmp_path):
    p, speakers = voice_film(tmp_path, {"tts": "inbox"}, {"inbox_fallback": "elevenlabs"})
    assert speakers and sorted(Pipeline._voiceless(p)) == speakers                 # none has a voice: all named
    p.cfg["voice"]["cast"] = {speakers[0]: "voiceid123"}
    assert sorted(Pipeline._voiceless(p)) == speakers[1:]
    lines = [b["block_id"] for blocks in __import__("nraiorbit.demo", fromlist=["SCENES"]).SCENES.values()
             for b in blocks if b["kind"] == "dialogue" and b.get("character_id") == speakers[1]]
    folder = p.p.root / "inbox" / "dialogue"
    folder.mkdir(parents=True)
    for line in lines:                                                      # recorded by hand: needs no voice
        (folder / f"{line}.mp3").write_bytes(b"x")
    assert sorted(Pipeline._voiceless(p)) == speakers[2:]
    p.cfg["voice"]["elevenlabs"] = {"default_voice": "fallbackid"}
    assert Pipeline._voiceless(p) == []                                     # one voice for everyone is a choice
    for providers, voice in (({"tts": "gemini"}, {}), ({"tts": "mock"}, {}), ({"tts": "inbox"}, {"inbox_fallback": "mock"})):
        q, _ = voice_film(tmp_path / providers["tts"] / str(len(voice)), providers, voice)
        assert Pipeline._voiceless(q) == [], providers                      # not ElevenLabs: nothing to check



def test_a_library_voice_keeps_its_elevenlabs_voice_for_the_provider_and_the_check(tmp_path):
    """voice.cast is per film; a library voice's elevenlabs_voice serves every film that casts it. The schema used to
    drop the field, so the provider's documented fallback never found it."""
    from nraiorbit.providers.elevenlabs import ElevenLabsTTS
    from nraiorbit.schemas import VoiceAsset
    v = VoiceAsset.model_validate({"id": "chintu_voice", "kind": "voice", "elevenlabs_voice": "abc123"})
    assert v.model_dump()["elevenlabs_voice"] == "abc123"
    tts = ElevenLabsTTS({"voices/chintu_voice@v1": v.model_dump()}, {"voice": {}}, {}, ledger=None,
                        cache_dir=tmp_path / "cache")
    line = SimpleNamespace(character_id="chintu")                          # all voice_id reads of the line
    assert tts.voice_id(line, "voices/chintu_voice@v1") == "abc123"


def test_the_voices_check_counts_only_lines_a_shot_carries_and_elevenlabs_speaks(tmp_path):
    from nraiorbit.demo import SCENES
    from nraiorbit.schemas import SceneScript
    p, speakers = voice_film(tmp_path, {"tts": "elevenlabs"}, {"cast": {s: "id" for s in speakers_of(SCENES)[1:]}})
    first = speakers_of(SCENES)[0]
    assert Pipeline._voiceless(p) == [first]
    # a line no shot carries is never rendered: its speaker needs no voice
    script = SceneScript.model_validate_json(next(p.p.scenes.glob("*.json")).read_text())
    ghost = script.blocks[0].model_copy(update={"block_id": "zz_b999", "kind": "dialogue", "character_id": "ghost"})
    (p.p.scenes / "zz999.json").write_text(script.model_copy(update={"scene_id": "zz999", "blocks": [ghost]})
                                           .model_dump_json())
    assert "ghost" not in Pipeline._voiceless(p)
    # the first speaker's lines all chanted, with a crowd provider: ElevenLabs speaks none of them
    lines = [b["block_id"] for blocks in SCENES.values() for b in blocks if b.get("character_id") == first]
    p.cfg["voice"]["crowd"] = {b: {"voices": 4} for b in lines}
    assert Pipeline._voiceless(p) == [first]                               # no crowd provider: ElevenLabs refuses
    p.cfg["voice"]["crowd_provider"] = "gemini"
    assert Pipeline._voiceless(p) == []


def speakers_of(scenes) -> list[str]:
    return sorted({b["character_id"] for blocks in scenes.values() for b in blocks
                   if b["kind"] == "dialogue" and b.get("character_id")})


def test_first_frames_call_an_animal_an_animal():
    from nraiorbit.providers.google_ai import keyframe_prompt
    text = keyframe_prompt(None, "In the forest.", [("Chintu", 2, True), ("Meera", 1, False), ("Arjun", 1)])
    assert "Chintu is the animal in reference images 1-2: keep exactly the same face, fur, markings" in text
    assert "Meera is the person in reference image 3" in text and "Arjun is the person in reference image 4" in text



def test_an_entry_another_film_made_under_the_same_name_is_never_taken_up(tmp_path):
    """Film "x" with a character "raja_y" and film "y-x" with "raja" both come to "raja_y_x": the entry records which
    film and character it was made for, so the second film gets a name of its own, never the first one's pictures."""
    bible = bible_with_a_monkey()
    raja = bible.characters[1].model_copy(update={"id": "raja"})
    raja_y = raja.model_copy(update={"id": "raja_y"})
    first = bible.model_copy(update={"characters": [raja_y]})
    second = bible.model_copy(update={"characters": [raja]})
    library = library_with(tmp_path)
    lock1 = CastLock(characters={"raja_y": CastEntry(character_asset="characters/raja_y@pending")})
    lock2 = CastLock(characters={"raja": CastEntry(character_asset="characters/raja@pending")})
    _, library, a = register_new_characters(first, lock1, library, film="x", log=lambda *_: None)
    _, library, b = register_new_characters(second, lock2, library, film="y-x", log=lambda *_: None)
    assert a == ["raja_y_x"] and b == ["raja_y_x_2"]
    _, _, again = register_new_characters(first, lock1, library, film="x", log=lambda *_: None)
    assert again == ["raja_y_x"]                                  # the first film still finds its own
