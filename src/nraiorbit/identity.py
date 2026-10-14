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

from dataclasses import dataclass, field
from pathlib import Path

from .library.registry import AssetRegistry
from .providers.google_ai import GeminiImages, ShotFiltered
from .schemas import CastLock, CharacterAsset, CharacterSpec, StoryBible

STUDIO = ("Photorealistic studio photograph, soft even light, plain light-grey background, sharp focus, "
          "natural skin texture, no makeup look, no text.")


def portrait_prompt(spec: CharacterSpec, era_region: str) -> str:
    ph = spec.physical
    marks = "" if ph.marks.lower() == "none" else f", {ph.marks}"
    return (f"Front-facing head-and-shoulders portrait of an entirely fictional {ph.age}-year-old Telugu {ph.gender} "
            f"from {era_region}: {ph.build} build, {ph.skin_tone} skin, {ph.face}, {ph.hair}{marks}. "
            f"Neutral expression, looking into the camera. Wearing {spec.costume_baseline}. {STUDIO} "
            "Must not resemble any real actor or public figure.")


def derived_portrait_prompt(spec: CharacterSpec, era_region: str, change: str) -> str:
    """Front portrait drawn from another character's portrait (the reference image): same person or a close family
    resemblance, with the manifest's change. The description's face details are left out: the reference is the face."""
    ph = spec.physical
    return (f"Front-facing head-and-shoulders portrait of the same person, or a close family resemblance, as in the "
            f"reference image, but {change.strip().rstrip('.')}. An entirely fictional {ph.age}-year-old Telugu "
            f"{ph.gender} from {era_region}, {ph.build} build. Neutral expression, looking into the camera. "
            f"Wearing {spec.costume_baseline}. {STUDIO} Must not resemble any real actor or public figure.")


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
                        force: bool = False, log=print, only: list[str] | None = None) -> dict[str, list[Path]]:
    made: dict[str, list[Path]] = {}

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

        if force or not front.exists():
            if job.reference is None:
                if make(portrait_prompt(spec, bible.era_and_region), front, aspect_ratio="3:4",
                        item=f"identity {spec.id} front"):
                    out.append(front)
            elif not job.reference.exists():
                log(f"  {spec.id}: no front portrait of {job.reference_asset} to draw from ({job.reference}); "
                    f"build that character first")
            elif make(derived_portrait_prompt(spec, bible.era_and_region, job.change or ""), front,
                      refs=[job.reference], aspect_ratio="3:4", item=f"identity {spec.id} front"):
                out.append(front)
        if not front.exists():
            continue                     # every other image is derived from the front portrait
        if (force or not three_q.exists()) and make(
                f"The same person as in the reference image, identical face, hair and skin tone, now in a "
                f"three-quarter view turned slightly to the left, neutral expression. {STUDIO}",
                three_q, refs=[front], aspect_ratio="3:4", item=f"identity {spec.id} three-quarter"):
            out.append(three_q)
        for costume_id, description, img in job.costumes:
            if (force or not img.exists()) and make(
                    f"The same person as in the reference image, identical face, build and skin tone. "
                    f"Full-body photograph, standing naturally, wearing {description}. {STUDIO}",
                    img, refs=[front], aspect_ratio="2:3", item=f"identity {spec.id} {costume_id}"):
                out.append(img)
        made[spec.id] = out
        log(f"  {spec.id}: {len(out)} new image(s) in {job.base / 'refs'}")
    return made
