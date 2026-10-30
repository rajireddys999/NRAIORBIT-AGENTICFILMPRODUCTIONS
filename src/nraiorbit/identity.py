"""Build character reference images (identity packs) with Gemini image generation.

For each cast character: one front portrait from the Story Bible description, then a
three-quarter view and one full-body image per costume, each generated *from the portrait*
so the face stays the same. Images go into the character's library folder, where the
shot pack and the Veo provider pick them up. Existing images are kept unless forced.

A character whose manifest has `identity_from` (a relative, or the same person at another
age) gets its front portrait drawn from that character's front portrait instead, changed as
the manifest says; referenced characters are built first.

Look at the images before generating video: a face you don't like here is in every shot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .library.registry import AssetRegistry
from .providers.google_ai import GeminiImages, ShotFiltered
from .schemas import CastLock, CharacterAsset, CharacterSpec, StoryBible

STUDIO = ("Photorealistic studio photograph, soft even light, plain light-grey background, sharp focus, "
          "natural skin texture, no makeup look, no text.")


ANIMAL_STUDIO = ("Photorealistic wildlife photograph, soft even light, plain light-grey background, sharp focus, "
                 "natural fur and skin texture, no text.")
def is_animal(spec: CharacterSpec) -> bool:
    """A character the story bible describes as an animal (fur, a tail, a macaque...): its pictures are wildlife
    photographs, never a person's portrait, and it is never given a voice automatically."""
    from .characters import animal_physical, marked_animal
    return marked_animal(spec) or animal_physical(spec.physical)


def _owner(notes: str) -> str:
    """The owner's own words for a character (looks.py), after the description."""
    return f" As the film's owner describes them: {notes.strip().rstrip('.')}." if notes and notes.strip() else ""


def portrait_prompt(spec: CharacterSpec, era_region: str, notes: str = "") -> str:
    ph = spec.physical
    marks = "" if ph.marks.lower() == "none" else f", {ph.marks}"
    if is_animal(spec):
        return (f"Front-facing head-and-shoulders photograph of an entirely fictional {ph.age}-year-old {ph.gender} "
                f"animal from {era_region}: {ph.build}, {ph.skin_tone}, {ph.face}, {ph.hair}{marks}.{_owner(notes)} "
                f"Calm, looking into the camera. {spec.costume_baseline}. {ANIMAL_STUDIO}")
    return (f"Front-facing head-and-shoulders portrait of an entirely fictional {ph.age}-year-old Telugu {ph.gender} "
            f"from {era_region}: {ph.build} build, {ph.skin_tone} skin, {ph.face}, {ph.hair}{marks}.{_owner(notes)} "
            f"Neutral expression, looking into the camera. Wearing {spec.costume_baseline}. {STUDIO} "
            "Must not resemble any real actor or public figure.")


def derived_portrait_prompt(spec: CharacterSpec, era_region: str, change: str, notes: str = "") -> str:
    """Front portrait drawn from another character's portrait (the reference image): same person or a close family
    resemblance, with the manifest's change. The description's face details are left out: the reference is the face."""
    ph = spec.physical
    if is_animal(spec):
        return (f"Front-facing head-and-shoulders photograph of the same animal as in the reference image, but "
                f"{change.strip().rstrip('.')}. An entirely fictional {ph.age}-year-old {ph.gender} animal from "
                f"{era_region}, {ph.build}.{_owner(notes)} Calm, looking into the camera. {spec.costume_baseline}. "
                f"{ANIMAL_STUDIO}")
    return (f"Front-facing head-and-shoulders portrait of the same person, or a close family resemblance, as in the "
            f"reference image, but {change.strip().rstrip('.')}. An entirely fictional {ph.age}-year-old Telugu "
            f"{ph.gender} from {era_region}, {ph.build} build.{_owner(notes)} Neutral expression, looking into the "
            f"camera. Wearing {spec.costume_baseline}. {STUDIO} Must not resemble any real actor or public figure.")


@dataclass
class IdentityJob:
    """The images of one cast character, and where its front portrait comes from."""

    spec: CharacterSpec
    asset: CharacterAsset
    base: Path
    front: Path
    three_q: Path
    costumes: list[tuple[str, str, Path]] = field(default_factory=list)   # (costume id, description, image)
    reference: Path | None = None        # identity_from: the referenced character's front portrait
    reference_asset: str | None = None
    change: str | None = None

    def images(self) -> list[Path]:
        return [self.front, self.three_q, *(img for *_, img in self.costumes)]


def _front_of(library: AssetRegistry, asset: CharacterAsset) -> Path:
    base = library.manifest_dirs[f"character:{asset.id}"]
    return base / asset.files.get("face_refs", "refs/face/") / "01_front.png"


def plan_identity_pack(bible: StoryBible, lock: CastLock, library: AssetRegistry, only: list[str] | None = None,
                       log=print) -> list[IdentityJob]:
    """The cast characters to build (only: Story Bible ids, nothing else), referenced characters first."""
    known = {spec.id for spec in bible.characters}
    unknown = sorted(set(only or []) - known)
    if unknown:
        raise ValueError(f"not in the Story Bible: {', '.join(unknown)} (characters: {', '.join(sorted(known))})")
    jobs: list[IdentityJob] = []
    for spec in bible.characters:
        if only and spec.id not in only:
            continue
        entry = lock.characters.get(spec.id)
        if entry is None or entry.character_asset.endswith("@pending"):
            log(f"  {spec.id}: not cast from the library yet, skipped")
            continue
        asset = library.resolve(entry.character_asset)
        base = library.manifest_dirs[f"character:{asset.id}"]
        front = _front_of(library, asset)
        job = IdentityJob(spec, asset, base, front, front.parent / "02_three_quarter.png", [
            (c.id, c.description, base / (c.refs or f"refs/costumes/{c.id}/") / "01_full_body.png")
            for c in getattr(asset, "costumes", [])])
        source = getattr(asset, "identity_from", None)
        if source is not None:
            if source.asset == asset.id:
                raise ValueError(f"character asset {asset.id}: identity_from refers to itself")
            ref_asset = library.get("character", source.asset)
            job.reference, job.reference_asset, job.change = _front_of(library, ref_asset), source.asset, source.change
        jobs.append(job)
    return _dependency_order(jobs)


def _dependency_order(jobs: list[IdentityJob]) -> list[IdentityJob]:
    by_asset = {job.asset.id: job for job in jobs}
    ordered: list[IdentityJob] = []
    state: dict[str, str] = {}

    def visit(job: IdentityJob, chain: tuple[str, ...]) -> None:
        if state.get(job.asset.id) == "done":
            return
        if state.get(job.asset.id) == "visiting":
            raise ValueError(f"identity_from loop: {' -> '.join((*chain, job.asset.id))}")
        state[job.asset.id] = "visiting"
        if job.reference_asset in by_asset:
            visit(by_asset[job.reference_asset], (*chain, job.asset.id))
        state[job.asset.id] = "done"
        ordered.append(job)

    for job in jobs:
        visit(job, ())
    return ordered


def images_to_make(jobs: list[IdentityJob], force: bool = False) -> list[Path]:
    """The images a build would really generate: missing ones (all with force) that have their source image."""
    made: set[Path] = set()
    todo: list[Path] = []
    for job in jobs:
        has_source = job.reference is None or job.reference.exists() or job.reference in made
        if (force or not job.front.exists()) and has_source:
            todo.append(job.front)
            made.add(job.front)
        if not (job.front.exists() or job.front in made):
            continue
        todo += [img for img in job.images()[1:] if force or not img.exists()]
    return todo


def build_identity_pack(bible: StoryBible, lock: CastLock, library: AssetRegistry, images: GeminiImages,
                        force: bool = False, log=print, only: list[str] | None = None,
                        notes: dict[str, str] | None = None) -> dict[str, list[Path]]:
    """`notes`: character id -> the owner's words for its pictures (looks.owner_words): in its portraits, and the
    build named in its full-body pictures."""
    made: dict[str, list[Path]] = {}
    notes = notes or {}

    def make(prompt: str, out: Path, **kw) -> bool:
        try:
            images.generate(prompt, out, **kw)
            return True
        except ShotFiltered as blocked:
            log(f"  skipped {out.name}: {blocked}. Adjust the character's description and run again.")
            return False

    for job in plan_identity_pack(bible, lock, library, only=only, log=log):
        spec, front, three_q = job.spec, job.front, job.three_q
        out: list[Path] = []
        who, studio = ("animal", ANIMAL_STUDIO) if is_animal(spec) else ("person", STUDIO)

        if getattr(job.asset, "owner_picture", False):
            if not front.exists():                       # the owner's picture is the face: never drawn over
                log(f"  {spec.id}: the owner's picture is missing from {front}; give it again in the control room")
                continue
        elif force or not front.exists():
            if job.reference is None:
                if make(portrait_prompt(spec, bible.era_and_region, notes.get(spec.id, "")), front, aspect_ratio="3:4",
                        item=f"identity {spec.id} front"):
                    out.append(front)
            elif not job.reference.exists():
                log(f"  {spec.id}: no front portrait of {job.reference_asset} to draw from ({job.reference}); "
                    f"build that character first")
            elif make(derived_portrait_prompt(spec, bible.era_and_region, job.change or "", notes.get(spec.id, "")), front,
                      refs=[job.reference], aspect_ratio="3:4", item=f"identity {spec.id} front"):
                out.append(front)
        if not front.exists():
            continue                     # every other image is derived from the front portrait
        if (force or not three_q.exists()) and make(
                f"The same {who} as in the reference image, identical face, hair and skin tone, now in a "
                f"three-quarter view turned slightly to the left, neutral expression. {studio}",
                three_q, refs=[front], aspect_ratio="3:4", item=f"identity {spec.id} three-quarter"):
            out.append(three_q)
        for costume_id, description, img in job.costumes:
            if (force or not img.exists()) and make(
                    f"The same {who} as in the reference image, identical face, build and skin tone. "
                    f"Full-body photograph, standing naturally, wearing {description}."
                    f"{_owner(notes[spec.id]) if spec.id in notes else ''} {studio}",
                    img, refs=[front], aspect_ratio="2:3", item=f"identity {spec.id} {costume_id}"):
                out.append(img)
        made[spec.id] = out
        log(f"  {spec.id}: {len(out)} new image(s) in {job.base / 'refs'}")
    return made


def character_manifest(spec: CharacterSpec, asset_id: str, film: str = "") -> dict:
    """A library entry for a character no library character fits, written from the story bible: the face, build and
    costume the identity images are made from. Owned and synthetic, so a production film may cast it."""
    ph = spec.physical
    age = "child" if ph.age < 13 else "teen" if ph.age < 20 else f"{ph.age // 10 * 10}s"
    costume = f"cst_{asset_id}_main"
    return {"id": asset_id, "kind": "character", "version": 1, "name": spec.name_en, "role": spec.role,
            "description": spec.personality, "tags": sorted({spec.role, ph.gender.lower(), age,
                                                             "animal" if is_animal(spec) else "human"}),
            "physical": ph.model_dump(),
            "costumes": [{"id": costume, "description": spec.costume_baseline, "refs": f"refs/costumes/{costume}/"}],
            "default_voice": None,
            "license": {"type": "owned", "commercial_use": True,
                        "source": "in-house synthetic identity pack, made by the runner from the story bible"},
            "consent": {"required": False}, "files": {"face_refs": "refs/face/"},
            "made_for": {"film": film, "character": spec.id}}


def new_character_id(cid: str, film: str) -> str:
    """A new character's library id carries its film's slug: two films planned at the same time (each runner tick
    restores the library and backs it up without deletes) can never write the same entry or the same pictures."""
    return re.sub(r"[^a-z0-9_]+", "_", f"{cid}_{film}".lower()).strip("_")


def _made_for(library: AssetRegistry, asset_id: str, film: str, cid: str) -> bool:
    """Whether a library entry is the one this film made for this character (its manifest says so)."""
    path = library.manifest_dirs.get(f"character:{asset_id}")
    try:
        made = (yaml.safe_load((path / "manifest.yaml").read_text(encoding="utf-8")) or {}).get("made_for") or {}
    except (OSError, AttributeError, yaml.YAMLError):
        return False
    return made.get("film") == film and made.get("character") == cid


def register_new_characters(bible: StoryBible, lock: CastLock, library: AssetRegistry, film: str,
                            log=print) -> tuple[CastLock, AssetRegistry, list[str]]:
    """Every character the casting left pending (no library character fits) gets a library entry of its own, named
    for this film, and the cast lock points at it: (the new lock, the library as it now stands, the ids registered).
    An entry this film made on an earlier, interrupted run is taken up again, never made twice."""
    specs = {spec.id: spec for spec in bible.characters}
    pending = [cid for cid, entry in lock.characters.items() if entry.character_asset.endswith("@pending")]
    if not pending:
        return lock, library, []
    added = []
    for cid in pending:
        spec = specs.get(cid)
        if spec is None:
            continue
        base, n = new_character_id(cid, film), 2
        asset_id = base
        while f"character:{asset_id}" in library.assets and not _made_for(library, asset_id, film, cid):
            asset_id, n = f"{base}_{n}", n + 1                   # another film's entry under the same name
        if f"character:{asset_id}" not in library.assets:          # else this film's own, from an interrupted run
            folder = library.root / "characters" / asset_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "manifest.yaml").write_text(yaml.safe_dump(character_manifest(spec, asset_id, film),
                                                                 sort_keys=False, allow_unicode=True), encoding="utf-8")
            log(f"  {cid}: a new library character, {asset_id} ({'animal' if is_animal(spec) else 'person'})")
        added.append((cid, asset_id))
    library = AssetRegistry.load(library.root)
    entries = dict(lock.characters)
    for cid, asset_id in added:
        asset = library.get("character", asset_id)
        entries[cid] = entries[cid].model_copy(update={
            "character_asset": library.ref(asset), "costumes": {c.id: c.description for c in asset.costumes}})
    return lock.model_copy(update={"characters": entries}), library, [asset_id for _, asset_id in added]
