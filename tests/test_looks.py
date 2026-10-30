"""The owner's looks for characters (looks.py): a picture that becomes the face, a body build and notes, given in the
control room until the cast is approved, taken in by the cast step, and kept in every still. All optional: a film
with no looks is cast and drawn as before."""

import io
import json
from pathlib import Path

import pytest
import yaml
from PIL import Image

from nraiorbit import looks
from nraiorbit.demo import STORY_BIBLE
from nraiorbit.identity import build_identity_pack, portrait_prompt
from nraiorbit.library.registry import AssetRegistry
from nraiorbit.providers.google_ai import keyframe_prompt
from nraiorbit.schemas import CastEntry, CastLock, StoryBible

FILM = "rain"


def picture(w=600, h=800, color=(180, 120, 90)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, format="JPEG")
    return out.getvalue()


def project(tmp_path: Path, given: dict | None = None, pictures: dict | None = None):
    """A film's root with the owner's looks copied in, the demo bible, a lock on someone else's library character,
    and that library."""
    root = tmp_path / FILM
    (root / "reports").mkdir(parents=True)
    bible = StoryBible.model_validate(STORY_BIBLE)
    hero = bible.characters[0]
    shared = tmp_path / "library" / "characters" / "hero_shared"
    (shared / "refs" / "face").mkdir(parents=True)
    (shared / "manifest.yaml").write_text(yaml.safe_dump({"id": "hero_shared", "kind": "character", "version": 1,
                                                          "license": {"type": "owned", "commercial_use": True},
                                                          "costumes": [{"id": "cst_old", "description": "old"}]}))
    (shared / "refs" / "face" / "01_front.png").write_bytes(b"another film's face")
    library = AssetRegistry.load(tmp_path / "library")
    lock = CastLock(characters={c.id: CastEntry(character_asset="characters/hero_shared@v1" if c is hero else
                                                f"characters/{c.id}@pending", costumes={"cst_old": "old"} if c is hero
                                                else {}) for c in bible.characters})
    if given is not None:
        looks.sync(root, yaml.safe_dump(given), lambda cid, name: (pictures or {}).get(name), log=lambda *_: None)
    return root, bible, lock, library, hero


def take(root, bible, lock, library):
    """looks.take, with the cast lock written where the film keeps it (as Pipeline._take_looks does)."""
    out = looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None)
    (root / "cast.lock.yaml").write_text(yaml.safe_dump(out[1].model_dump()))
    return out


def test_a_film_without_looks_is_cast_as_before(tmp_path):
    root, bible, lock, library, _ = project(tmp_path)
    assert looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None) == (bible, lock, library, [])
    assert not (root / looks.RECORD).exists()


def test_the_owners_picture_becomes_a_private_face_with_its_build_and_notes(tmp_path):
    png = looks.normalize_picture(picture())
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {
        "image": "hero_1.png", "image_sha": "abc", "rights": "own", "build": "very_muscular",
        "notes": "a thick moustache", "by": "owner@example.com", "at": "2026-10-30T10:00:00+00:00"}},
        {"hero_1.png": png})
    bible2, lock2, library2, changed = take(root, bible, lock, library)
    assert changed == [hero.id]
    ref = lock2.characters[hero.id].character_asset
    assert ref.startswith(f"characters/{hero.id}_{FILM}_look_") and ref != lock.characters[hero.id].character_asset
    asset = library2.resolve(ref)
    assert asset.private and asset.consent.required and "owner@example.com" in asset.consent.document_ref
    folder = library2.manifest_dirs[f"character:{asset.id}"]
    assert (folder / "refs" / "face" / "01_front.png").read_bytes() == png          # the face is the owner's picture
    assert not (folder / "refs" / "face" / "02_three_quarter.png").exists()          # the rest is drawn from it
    assert (tmp_path / "library" / "characters" / "hero_shared" / "refs" / "face" / "01_front.png").read_bytes() \
        == b"another film's face"                                                     # the shared one is untouched
    built = next(c for c in bible2.characters if c.id == hero.id).physical.build
    assert built == "very muscular, powerful bodybuilder physique"
    assert looks.owner_words(root) == {hero.id: f"{built} build; a thick moustache"}
    # taken once: the next run changes nothing
    assert looks.take(root, bible2, lock2, library2, film=FILM, log=lambda *_: None)[3] == []


def hero_id() -> str:
    return StoryBible.model_validate(STORY_BIBLE).characters[0].id


def test_a_changed_look_is_a_new_character_and_removing_it_goes_back(tmp_path):
    first = {hero_id(): {"build": "athletic", "notes": "a scar over the left eyebrow"}}
    root, bible, lock, library, hero = project(tmp_path, first)
    as_written = hero.physical.build
    bible2, lock2, library2, _ = looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None)
    one = lock2.characters[hero.id].character_asset
    looks.sync(root, yaml.safe_dump({hero.id: {"build": "stocky", "notes": "a scar over the left eyebrow"}}),
               lambda cid, name: None)
    bible3, lock3, library3, changed = looks.take(root, bible2, lock2, library2, film=FILM, log=lambda *_: None)
    two = lock3.characters[hero.id].character_asset
    assert changed == [hero.id] and two != one and f"character:{one.split('/')[1].split('@')[0]}" in library3.assets
    assert next(c for c in bible3.characters if c.id == hero.id).physical.build == "stocky"
    # the list unreadable (empty): nothing is undone
    looks.sync(root, "", lambda cid, name: None)
    assert looks.take(root, bible3, lock3, library3, film=FILM, log=lambda *_: None)[3] == []
    # removed by the owner: the casting and the build it had before
    looks.sync(root, yaml.safe_dump({hero.id: {"removed": True}}), lambda cid, name: None)
    bible4, lock4, _, changed = looks.take(root, bible3, lock3, library3, film=FILM, log=lambda *_: None)
    assert changed == [hero.id]
    assert lock4.characters[hero.id].character_asset == "characters/hero_shared@v1"
    assert lock4.characters[hero.id].costumes == {"cst_old": "old"}
    assert next(c for c in bible4.characters if c.id == hero.id).physical.build == as_written
    assert hero.id not in looks.record(root)


def test_a_picture_not_copied_yet_waits(tmp_path):
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"image": "hero_2.png", "image_sha": "x",
                                                                     "rights": "ai"}}, {})
    assert looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None)[3] == []
    given = yaml.safe_load((root / looks.FOLDER / looks.CHOICES).read_text())
    assert looks.waiting(given, looks.record(root), {hero.id}) == [hero.id]


def test_casting_never_offers_a_private_look_to_another_film(tmp_path):
    from nraiorbit.agents.production import CastingAgent, _character_query_tags
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"build": "lean"}})
    _, _, library2, _ = looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None)
    folder = next(p for p in (tmp_path / "library" / "characters").iterdir() if "_look_" in p.name)
    manifest = yaml.safe_load((folder / "manifest.yaml").read_text())
    manifest["tags"] = _character_query_tags(hero)                      # a perfect match for the next film's hero
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    library3 = AssetRegistry.load(tmp_path / "library")

    class Agent(CastingAgent):
        def __init__(self):
            pass

        def brief(self, bible, spec):
            return None
    lock2, _ = Agent().cast(bible, library3)
    assert "_look_" not in lock2.characters[hero.id].character_asset


def test_pictures_are_cleaned_and_odd_files_refused():
    png = looks.normalize_picture(picture(3000, 4000))
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and max(img.size) == looks.MAX_SIDE and not img.info.get("exif")
    with pytest.raises(ValueError, match="too small"):
        looks.normalize_picture(picture(100, 120))
    with pytest.raises(ValueError, match="not a picture"):
        looks.normalize_picture(b"%PDF-1.4 not an image")


def test_the_look_folder_follows_the_bucket(tmp_path):
    fetched = []
    root = tmp_path / FILM

    def fetch(cid, name):
        fetched.append(name)
        return b"png"
    looks.sync(root, yaml.safe_dump({"a": {"image": "a_1.png"}, "b": {"notes": "tall"}}), fetch)
    looks.sync(root, yaml.safe_dump({"a": {"image": "a_1.png"}, "b": {"notes": "tall"}}), fetch)
    assert fetched == ["a_1.png"]                                        # fetched once
    looks.sync(root, yaml.safe_dump({"a": {"image": "a_2.png"}}), fetch)
    assert sorted(p.name for p in (root / looks.FOLDER).iterdir()) == ["a_2.png", "looks.yaml"]
    looks.sync(root, yaml.safe_dump({"a": {"image": "../x.png"}}), fetch)
    assert fetched == ["a_1.png", "a_2.png"]                             # never a path outside the folder
    looks.sync(root, "", fetch)
    assert not (root / looks.FOLDER).exists()


def test_the_owners_words_reach_the_portraits_and_every_still(tmp_path):
    bible = StoryBible.model_validate(STORY_BIBLE)
    hero = bible.characters[0]
    assert "As the film's owner describes them: a thick moustache." in portrait_prompt(
        hero, bible.era_and_region, "a thick moustache")
    assert "owner" not in portrait_prompt(hero, bible.era_and_region)
    prompt = keyframe_prompt(None, "He rows.", [(hero.name_en, 2)], {hero.name_en: "stocky build; a thick moustache"})
    assert f"{hero.name_en} is the person in reference images 1-2: keep exactly the same face, skin tone, hair and " \
           "build (stocky build; a thick moustache)" in prompt
    assert "(" not in keyframe_prompt(None, "He rows.", [(hero.name_en, 2)])

    # the full-body costume picture names the owner's build too
    root, bible, lock, library, hero = project(tmp_path, {hero.id: {"build": "very_muscular"}})
    bible2, lock2, library2, _ = take(root, bible, lock, library)
    asked = []

    class Images:
        def generate(self, prompt, out, **kw):
            asked.append(prompt)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"img")
    build_identity_pack(bible2, lock2, library2, Images(), log=lambda *_: None, only=[hero.id],
                        notes=looks.owner_words(root))
    assert any("Full-body" in p and "very muscular, powerful bodybuilder physique build" in p for p in asked)
    assert any("Front-facing" in p and "very muscular" in p for p in asked)


def test_the_record_is_kept_with_the_film(tmp_path):
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"notes": "tall"}})
    looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None)
    rec = json.loads((root / "reports" / "looks.json").read_text())       # reports/ goes to the bucket every run
    assert rec[hero.id]["before"]["character_asset"] == "characters/hero_shared@v1"


def test_a_film_with_no_looks_draws_and_casts_exactly_as_before(tmp_path):
    """The existing flow: no looks folder, no record. The cast step writes nothing; every prompt is the same text it
    was before looks existed; casting still finds library characters."""
    from nraiorbit.identity import derived_portrait_prompt
    bible = StoryBible.model_validate(STORY_BIBLE)
    hero = bible.characters[0]
    ph = hero.physical
    marks = "" if ph.marks.lower() == "none" else f", {ph.marks}"
    assert portrait_prompt(hero, bible.era_and_region) == (
        f"Front-facing head-and-shoulders portrait of an entirely fictional {ph.age}-year-old Telugu {ph.gender} "
        f"from {bible.era_and_region}: {ph.build} build, {ph.skin_tone} skin, {ph.face}, {ph.hair}{marks}. "
        f"Neutral expression, looking into the camera. Wearing {hero.costume_baseline}. "
        "Photorealistic studio photograph, soft even light, plain light-grey background, sharp focus, natural skin "
        "texture, no makeup look, no text. Must not resemble any real actor or public figure.")
    assert derived_portrait_prompt(hero, bible.era_and_region, "older") == derived_portrait_prompt(
        hero, bible.era_and_region, "older", "")
    assert keyframe_prompt(None, "He rows.", [(hero.name_en, 1), ("Chintu", 2, True)]) == (
        f"Create one photorealistic 16:9 film still, the first frame of this shot. {hero.name_en} is the person in "
        "reference image 1: keep exactly the same face, skin tone, hair and build; Chintu is the animal in reference "
        "images 2-3: keep exactly the same face, fur, markings and build. He rows. Do not add any text, captions or "
        "borders.")
    root, bible, lock, library, _ = project(tmp_path)
    (root / "cast.lock.yaml").write_text(yaml.safe_dump(lock.model_dump()))
    assert looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None)[3] == []
    assert looks.owner_words(root) == {}
    assert not (root / looks.RECORD).exists() and not (root / looks.TRIED).exists()
    asked = []

    class Images:
        def generate(self, prompt, out, **kw):
            asked.append(prompt)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"img")
    build_identity_pack(bible, lock, library, Images(), log=lambda *_: None, only=[bible.characters[0].id])
    assert asked and not any("owner" in p for p in asked)
    hits = library.search("character", tags=[], k=10)                        # nothing private: nothing filtered
    assert [h.asset.id for h in hits] == [h.asset.id for h in library.search("character", tags=[], k=10,
                                                                               include_private=False)]


def test_a_recast_character_never_carries_the_old_looks_words(tmp_path):
    """Send back to Cast puts another character where the look was: until the look is taken again, its words reach
    no picture and the control room shows it waiting."""
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"notes": "a thick moustache"}})
    _, lock2, _, _ = take(root, bible, lock, library)
    assert looks.owner_words(root) == {hero.id: "a thick moustache"}
    (root / "cast.lock.yaml").write_text(yaml.safe_dump(lock.model_dump()))      # recast: the shared one again
    assert looks.owner_words(root) == {}
    given = yaml.safe_load((root / looks.FOLDER / looks.CHOICES).read_text())
    assert looks.waiting(given, looks.record(root), {hero.id}, lock.model_dump()["characters"]) == [hero.id]
    assert looks.waiting(given, looks.record(root), {hero.id}, lock2.model_dump()["characters"]) == []


def test_a_lost_face_is_put_back_from_the_owners_picture(tmp_path):
    """A restore that brought the look's entry but not its picture: the owner's picture goes back, never an AI face."""
    png = looks.normalize_picture(picture())
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"image": "hero_1.png", "image_sha": "a",
                                                                     "rights": "ai"}}, {"hero_1.png": png})
    bible2, lock2, library2, _ = take(root, bible, lock, library)
    front = next((tmp_path / "library" / "characters").glob("*_look_*")) / "refs" / "face" / "01_front.png"
    front.unlink()
    looks.keep_faces(root, lock2, library2, log=lambda *_: None)          # every run, approved or not
    assert front.read_bytes() == png
    # and when it cannot be put back, no AI face is drawn in its place, even when forced
    front.unlink()
    (root / looks.FOLDER / "hero_1.png").unlink()
    looks.keep_faces(root, lock2, library2, log=lambda *_: None)
    asked = []

    class Images:
        def generate(self, prompt, out, **kw):
            asked.append(prompt)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"ai")
    for force in (False, True):
        build_identity_pack(bible2, lock2, library2, Images(), force=force, log=lambda *_: None, only=[hero_id()])
    assert not front.exists() and asked == []


def test_a_build_or_notes_change_keeps_the_face_the_owner_saw(tmp_path):
    """No picture: the new look is drawn from the character's current face, with the owner's build."""
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"build": "muscular"}})
    _, lock2, library2, _ = take(root, bible, lock, library)
    asset = library2.resolve(lock2.characters[hero.id].character_asset)
    assert asset.identity_from.asset == "hero_shared" and "muscular, broad-shouldered build" in asset.identity_from.change
    root3, bible, lock, library, _ = project(tmp_path / "c", {hero.id: {"notes": "a thick moustache"}})
    _, lock4, library4, _ = take(root3, bible, lock, library)
    assert library4.resolve(lock4.characters[hero.id].character_asset).identity_from.change == \
        "changed only as the film's owner describes"
    # a character with no face yet is drawn from its description, as before
    root2, bible, lock, library, _ = project(tmp_path / "b", {"meera": {"build": "slim"}})
    _, lock3, library3, _ = take(root2, bible, lock, library)
    assert library3.resolve(lock3.characters["meera"].character_asset).identity_from is None


def test_a_look_that_cannot_be_taken_says_why_and_stops_holding_the_approval(tmp_path):
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"image": "hero_9.png", "image_sha": "z",
                                                                     "rights": "ai"}}, {})
    take(root, bible, lock, library)
    tried = json.loads((root / looks.TRIED).read_text())
    assert tried[hero.id]["why"] == "its picture could not be read from the bucket"
    given = yaml.safe_load((root / looks.FOLDER / looks.CHOICES).read_text())
    assert looks.waiting(given, looks.record(root), {hero.id}, lock.model_dump()["characters"], tried) == []
    # a name that points anywhere but the looks folder is never used
    (root / looks.FOLDER / looks.CHOICES).write_text(yaml.safe_dump({hero.id: {"image": "../cast.lock.yaml"}}))
    assert looks.take(root, bible, lock, library, film=FILM, log=lambda *_: None)[3] == []


def test_a_huge_picture_is_refused_before_it_is_decoded():
    out = io.BytesIO()
    Image.new("L", (8000, 6000)).save(out, format="PNG")
    with pytest.raises(ValueError, match="too large"):
        looks.normalize_picture(out.getvalue())


def test_an_animal_keeps_its_animal_words_and_is_drawn_as_an_animal(tmp_path):
    from nraiorbit.identity import derived_portrait_prompt, is_animal
    root, bible, lock, library, hero = project(tmp_path)
    dog = hero.model_copy(update={"physical": hero.physical.model_copy(update={
        "build": "small, wiry, with a curled tail", "skin_tone": "brown fur", "face": "a black snout"})})
    bible = bible.model_copy(update={"characters": [dog, *bible.characters[1:]]})
    assert is_animal(dog)
    looks.sync(root, yaml.safe_dump({dog.id: {"build": "slim", "notes": "a black patch over the left eye"}}),
               lambda cid, name: None)
    bible2, lock2, library2, _ = take(root, bible, lock, library)
    still = next(c for c in bible2.characters if c.id == dog.id)
    assert still.physical.build == "small, wiry, with a curled tail" and is_animal(still)
    assert looks.owner_words(root) == {dog.id: "slim build; a black patch over the left eye"}
    prompt = derived_portrait_prompt(still, bible.era_and_region, "with a slim build", "a black patch")
    assert "same animal" in prompt and "person" not in prompt and "Telugu" not in prompt


def test_a_new_look_is_drawn_from_the_ais_own_character_never_a_removed_photo(tmp_path):
    png = looks.normalize_picture(picture())
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"image": "hero_1.png", "image_sha": "a",
                                                                     "rights": "own"}}, {"hero_1.png": png})
    _, lock2, library2, _ = take(root, bible, lock, library)
    # the owner removed the picture and chose a build before any run saw the removal
    looks.sync(root, yaml.safe_dump({hero.id: {"build": "stocky"}}), lambda cid, name: None)
    _, lock3, library3, _ = take(root, bible, lock2, library2)
    asset = library3.resolve(lock3.characters[hero.id].character_asset)
    assert asset.identity_from.asset == "hero_shared" and not asset.owner_picture


def test_a_look_waits_when_the_library_did_not_come_down(tmp_path):
    root, bible, lock, library, hero = project(tmp_path, {hero_id(): {"build": "lean"}})
    import shutil
    shutil.rmtree(tmp_path / "library" / "characters" / "hero_shared")
    bare = AssetRegistry.load(tmp_path / "library")
    assert looks.take(root, bible, lock, bare, film=FILM, log=lambda *_: None)[3] == []
    assert not list((tmp_path / "library" / "characters").glob("*_look_*"))   # nothing half-made to keep later


def test_a_large_webp_is_refused_where_a_jpeg_of_that_size_is_fine():
    out = io.BytesIO()
    Image.new("RGB", (5000, 4000)).save(out, format="WEBP", quality=10)
    with pytest.raises(ValueError, match="under 16 megapixels"):
        looks.normalize_picture(out.getvalue())
    assert looks.normalize_picture(picture(5000, 4000))
