"""Build character reference images (identity packs) with Gemini image generation.

For each cast character: one front portrait from the Story Bible description, then a
three-quarter view and one full-body image per costume, each generated *from the portrait*
so the face stays the same. Images go into the character's library folder, where the
shot pack and the Veo provider pick them up. Existing images are kept unless forced.

Look at the images before generating video: a face you don't like here is in every shot.
"""

from __future__ import annotations

from pathlib import Path

from .library.registry import AssetRegistry
from .providers.google_ai import GeminiImages, ShotFiltered
from .schemas import CastLock, CharacterSpec, StoryBible

STUDIO = ("Photorealistic studio photograph, soft even light, plain light-grey background, sharp focus, "
          "natural skin texture, no makeup look, no text.")


def portrait_prompt(spec: CharacterSpec, era_region: str) -> str:
    ph = spec.physical
    marks = "" if ph.marks.lower() == "none" else f", {ph.marks}"
    return (f"Front-facing head-and-shoulders portrait of an entirely fictional {ph.age}-year-old Telugu {ph.gender} "
            f"from {era_region}: {ph.build} build, {ph.skin_tone} skin, {ph.face}, {ph.hair}{marks}. "
            f"Neutral expression, looking into the camera. Wearing {spec.costume_baseline}. {STUDIO} "
            "Must not resemble any real actor or public figure.")


def build_identity_pack(bible: StoryBible, lock: CastLock, library: AssetRegistry, images: GeminiImages,
                        force: bool = False, log=print) -> dict[str, list[Path]]:
    made: dict[str, list[Path]] = {}

    def make(prompt: str, out: Path, **kw) -> bool:
        try:
            images.generate(prompt, out, **kw)
            return True
        except ShotFiltered as blocked:
            log(f"  skipped {out.name}: {blocked}. Adjust the character's description and run again.")
            return False

    for spec in bible.characters:
        entry = lock.characters.get(spec.id)
        if entry is None or entry.character_asset.endswith("@pending"):
            log(f"  {spec.id}: not cast from the library yet, skipped")
            continue
        asset = library.resolve(entry.character_asset)
        base = library.manifest_dirs[f"character:{asset.id}"]
        face_dir = base / asset.files.get("face_refs", "refs/face/")
        front, three_q = face_dir / "01_front.png", face_dir / "02_three_quarter.png"
        out: list[Path] = []

        if (force or not front.exists()) and make(portrait_prompt(spec, bible.era_and_region), front,
                                                   aspect_ratio="3:4", item=f"identity {spec.id} front"):
            out.append(front)
        if not front.exists():
            continue                     # every other image is derived from the front portrait
        if (force or not three_q.exists()) and make(
                f"The same person as in the reference image, identical face, hair and skin tone, now in a "
                f"three-quarter view turned slightly to the left, neutral expression. {STUDIO}",
                three_q, refs=[front], aspect_ratio="3:4", item=f"identity {spec.id} three-quarter"):
            out.append(three_q)
        for costume in getattr(asset, "costumes", []):
            folder = base / (costume.refs or f"refs/costumes/{costume.id}/")
            img = folder / "01_full_body.png"
            if (force or not img.exists()) and make(
                    f"The same person as in the reference image, identical face, build and skin tone. "
                    f"Full-body photograph, standing naturally, wearing {costume.description}. {STUDIO}",
                    img, refs=[front], aspect_ratio="2:3", item=f"identity {spec.id} {costume.id}"):
                out.append(img)
        made[spec.id] = out
        log(f"  {spec.id}: {len(out)} new image(s) in {base / 'refs'}")
    return made
