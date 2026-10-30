"""The owner's look for a character, given in the control room until the cast is approved: a picture that becomes
the character's face, a body build, and notes on how the character looks. All of it is optional: a character with no
look is cast and drawn as before.

The control room keeps the looks in the bucket (stories/<slug>.looks.yaml; the pictures in stories/<slug>.looks/),
each tick copies them into the project's looks/, and the cast step takes them in before its pictures are drawn
(Pipeline._cast):

    picture  the character's front portrait; the other angles and every still are drawn from it
    build    the story bible's build for the character, so the portraits, the director and every still agree
    notes    added to the character's portraits and to every still the character is in

A look gets a library character of its own, named for the look (a changed look is a new one, so its pictures never
mix with the last one's, on this disk or in the bucket) and private: casting never offers it to another film. A
character whose look the owner removed (`removed: true`) goes back to the character it had before. What was taken, and what it
replaced, is in reports/looks.json; nothing is taken once the cast is approved."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
from pathlib import Path

import yaml

FOLDER = "looks"             # in the project: looks.yaml and the pictures it names, copied from the bucket each tick
CHOICES = "looks.yaml"
RECORD = "reports/looks.json"
TRIED = "reports/looks_waiting.json"   # looks the cast step could not take, and why (shown on the film page)
NOTES_MAX = 600
PICTURE_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\.png")
BUILDS = {                   # the control room's choices: key -> (label, the words the prompts use)
    "slim": ("Slim", "slim"),
    "lean": ("Lean", "lean"),
    "average": ("Average", "average"),
    "athletic": ("Athletic", "athletic"),
    "muscular": ("Muscular", "muscular, broad-shouldered"),
    "very_muscular": ("Very muscular (bodybuilder)", "very muscular, powerful bodybuilder physique"),
    "stocky": ("Stocky", "stocky"),
    "heavy": ("Heavy-set", "heavy-set"),
    "plump": ("Plump", "plump"),
}
RIGHTS = {                   # what the owner confirms with a picture
    "own": "I own this picture and have the person's permission to use their face in this film",
    "ai": "This picture is AI-made: no real person",
}


MAX_PICTURE = 15 * 1024 * 1024
MAX_PIXELS = {"JPEG": 40_000_000, "MPO": 40_000_000}   # a small file can still be a huge picture: a JPEG is decoded
OTHER_PIXELS = 16_000_000    # at the size it is kept; WebP and PNG decode whole, and must fit the control room
MIN_SIDE = 256               # smaller than this, a face cannot be drawn from it
MAX_SIDE = 2048


def normalize_picture(data: bytes) -> bytes:
    """An uploaded picture as the PNG the cast step uses: upright, RGB, at most MAX_SIDE a side, and nothing else
    from the file (no location or camera details). ValueError, in the owner's words, when it cannot be used."""
    import io

    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
            size, kind = probe.size, probe.format
        limit = MAX_PIXELS.get(kind, OTHER_PIXELS)
        if size[0] * size[1] > limit:
            raise ValueError(f"That picture is {size[0]}x{size[1]}, too large to work with; send one under "
                             f"{limit // 1_000_000} megapixels (a phone photo saved as JPEG is fine); nothing changed.")
        img = Image.open(io.BytesIO(data))
        if img.format in MAX_PIXELS:
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))         # decoded near the size it is kept at
        img = ImageOps.exif_transpose(img)
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("That file is not a picture we can read (use a JPEG, PNG or WebP); nothing changed.") from None
    if min(img.size) < MIN_SIDE:
        raise ValueError(f"That picture is {img.size[0]}x{img.size[1]}: too small to draw a face from (at least "
                         f"{MIN_SIDE} pixels a side); nothing changed.")
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def cast_gate_waits(cfg: dict) -> bool:
    """Whether the film's cast waits for the owner (its gate on, not approved by itself): only then are looks taken."""
    g = (cfg.get("review_gates") or {}).get("cast") or {}
    return bool(g.get("enabled", True)) and not g.get("auto_approve", False)


def in_film(look: dict, taken: dict, asset_now: str | None) -> bool:
    """Whether this saved look is the one the cast uses now: taken, and the cast lock (`asset_now`: the character's
    entry) still points at it; a recast after Send back to Cast puts another there until the look is taken again."""
    return taken.get("fp") == fingerprint(look) and bool(taken.get("asset_ref")) and asset_now == taken["asset_ref"]


def waiting(given: dict, taken: dict, characters, lock: dict | None = None, tried: dict | None = None) -> list[str]:
    """The characters whose saved look (the bucket's looks.yaml) the cast step has yet to take: neither in the film
    (`lock`: the cast lock's characters) nor tried and found impossible (`tried`: reports/looks_waiting.json)."""
    out = []
    for cid, look in sorted(given.items()):
        if cid not in characters or not isinstance(look, dict):
            continue
        if look.get("removed"):
            if cid in taken:
                out.append(cid)
            continue
        here = ((lock or {}).get(cid) or {}).get("character_asset")
        if not in_film(look, taken.get(cid) or {}, here) and (tried or {}).get(cid, {}).get("fp") != fingerprint(look):
            out.append(cid)
    return out


def choices(root: Path) -> dict[str, dict]:
    """The owner's looks as copied into the project: character id -> look."""
    try:
        data = yaml.safe_load((Path(root) / FOLDER / CHOICES).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def fingerprint(look: dict) -> str:
    """What makes two looks the same: the picture, the build and the notes (not who saved it, or when)."""
    key = [look.get("image_sha") or "", look.get("build") or "", (look.get("notes") or "").strip()]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:10]


def record(root: Path) -> dict[str, dict]:
    try:
        data = json.loads((Path(root) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def owner_words(root: Path) -> dict[str, str]:
    """character id -> the owner's words for its pictures (build and notes), from the looks the cast step took and
    the cast still uses (never for a character recast in their place)."""
    try:
        cast = (yaml.safe_load((Path(root) / "cast.lock.yaml").read_text(encoding="utf-8")) or {}).get("characters")
    except (OSError, yaml.YAMLError, AttributeError):
        cast = None
    out = {}
    for cid, r in record(root).items():
        if not isinstance(cast, dict) or ((cast.get(cid) or {}).get("character_asset") != r.get("asset_ref")):
            continue
        parts = [f"{r['owner_build_words']} build"] if r.get("owner_build_words") else []
        if (r.get("notes") or "").strip():
            parts.append(r["notes"].strip())
        if parts:
            out[cid] = "; ".join(parts)
    return out


def sync(root: Path, text: str, fetch_picture, log=print) -> None:
    """Copy the owner's looks (the bucket's looks.yaml `text`) into the project's looks/, each picture fetched once
    (`fetch_picture(name)` -> bytes or None) and kept while its name is listed. No text: no looks."""
    folder = Path(root) / FOLDER
    if not text.strip():
        if folder.exists():
            shutil.rmtree(folder)
        return
    folder.mkdir(parents=True, exist_ok=True)
    (folder / CHOICES).write_text(text, encoding="utf-8")
    wanted = {str(v["image"]) for v in choices(root).values() if v.get("image")}
    for name in sorted(wanted):
        if not PICTURE_NAME.fullmatch(name) or (folder / name).exists():
            continue
        data = fetch_picture(name)
        if data:
            (folder / name).write_bytes(data)
        else:
            log(f"  look picture {name} could not be read from the bucket; the cast step waits for it")
    for p in folder.iterdir():
        if p.name != CHOICES and p.name not in wanted:
            p.unlink()


def take(root: Path, bible, lock, library, film: str, log=print):
    """The owner's looks into the cast: (bible, lock, library, the character ids changed). Run before the cast's
    pictures are drawn and only until the cast is approved. A look already taken changes nothing; a removed look
    puts the character back as it was."""
    from .identity import _front_of, character_manifest, is_animal, new_character_id
    from .library.registry import AssetRegistry

    root = Path(root)
    looks, rec = choices(root), record(root)
    try:
        tried = json.loads((root / TRIED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tried = {}
    tried = tried if isinstance(tried, dict) else {}
    was_tried = dict(tried)
    specs = {s.id: s for s in bible.characters}
    entries, changed, now = dict(lock.characters), [], datetime.datetime.now(datetime.timezone.utc)
    new_specs = dict(specs)
    for cid in sorted(set(looks) | set(rec)):
        spec, entry = specs.get(cid), entries.get(cid)
        if spec is None or entry is None:
            continue
        look, mine = looks.get(cid), rec.get(cid)
        current = spec.physical.build
        in_use = bool(mine) and entry.character_asset == mine.get("asset_ref")   # not after a recast
        if look is None:                                   # nothing said (or the list unreadable): as it is
            continue
        if look.get("removed"):                            # the owner chose the AI's own look again
            if mine:
                build = mine.get("as_written_build") if current == mine.get("build") else current
                new_specs[cid] = _with_build(spec, build)
                if in_use:
                    entries[cid] = entry.model_copy(update=mine.get("before") or {})
                rec.pop(cid)
                changed.append(cid)
                log(f"  {cid}: the owner's look removed, back to {entries[cid].character_asset}")
            continue
        fp = fingerprint(look)
        name = str(look.get("image") or "")
        picture = root / FOLDER / name if name else None
        if picture is not None and not (PICTURE_NAME.fullmatch(name) and picture.is_file()):
            tried[cid] = {"fp": fp, "why": "its picture could not be read from the bucket"}
            log(f"  {cid}: the owner's picture is not here; the look waits for a run that has it")
            continue
        tried.pop(cid, None)
        as_written = (mine or {}).get("as_written_build") if mine and current == mine.get("build") else current
        chosen = BUILDS[look["build"]][1] if look.get("build") in BUILDS else None
        # an animal keeps its written build (those words say it is an animal); the owner's build goes with the notes
        build = chosen if chosen and not is_animal(spec) else as_written
        asset_id = f"{new_character_id(cid, film)}_look_{fp}"
        folder = library.root / "characters" / asset_id
        if in_use and mine.get("fp") == fp and current == build:
            continue                                       # taken already
        base_ref = mine["before"]["character_asset"] if in_use else entry.character_asset   # the AI's own casting
        base = library.assets.get(_asset_key(base_ref))
        if f"character:{asset_id}" not in library.assets and base is None and not base_ref.endswith("@pending"):
            log(f"  {cid}: {base_ref} is not in the library on this run; the look waits for a run that has it")
            continue
        spec = new_specs[cid] = _with_build(spec, build)
        if f"character:{asset_id}" not in library.assets:
            base_front = _front_of(library, base) if base is not None and not getattr(base, "private", False) else None
            (folder / "refs" / "face").mkdir(parents=True, exist_ok=True)
            manifest = character_manifest(spec, asset_id, film)
            manifest["private"] = True
            manifest["owner_look"] = fp
            manifest["owner_picture"] = picture is not None
            if picture is not None:
                shutil.copyfile(picture, folder / "refs" / "face" / "01_front.png")
                manifest["license"]["source"] = "the owner's picture, given in the control room"
                if look.get("rights") == "own":
                    manifest["consent"] = {"required": True, "document_ref": (
                        f"the owner confirmed permission in the control room ({look.get('by') or 'owner'}, "
                        f"{look.get('at') or now.isoformat(timespec='seconds')})")}
            elif base_front is not None and base_front.exists():     # the face the owner saw, with the new build
                manifest["identity_from"] = {"asset": base.id, "change": (
                    f"with {'an' if chosen[:1].lower() in 'aeiou' else 'a'} {chosen} build" if chosen else
                    "changed only as the film's owner describes")}
            elif base is not None and getattr(base, "identity_from", None) is not None:
                manifest["identity_from"] = base.identity_from.model_dump()     # a relative stays a relative
            (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                                                  encoding="utf-8")
            library = AssetRegistry.load(library.root)
        asset = library.get("character", asset_id)
        before = mine["before"] if in_use else {"character_asset": entry.character_asset,
                                                "costumes": dict(entry.costumes)}
        entries[cid] = entry.model_copy(update={"character_asset": library.ref(asset),
                                                "costumes": {c.id: c.description for c in asset.costumes}})
        rec[cid] = {"fp": fp, "asset_ref": library.ref(asset), "picture": picture is not None,
                    "rights": look.get("rights") if picture is not None else None, "build": build,
                    "owner_build_words": chosen, "as_written_build": as_written,
                    "notes": (look.get("notes") or "").strip()[:NOTES_MAX], "before": before,
                    "taken": now.isoformat(timespec="seconds")}
        changed.append(cid)
        log(f"  {cid}: the owner's look ({'picture, ' if picture is not None else ''}{build} build"
            f"{', notes' if rec[cid]['notes'] else ''}) -> {asset_id}")
    if tried != was_tried:
        (root / TRIED).parent.mkdir(parents=True, exist_ok=True)
        (root / TRIED).write_text(json.dumps(tried, indent=1), encoding="utf-8")
    if changed:
        (root / RECORD).parent.mkdir(parents=True, exist_ok=True)
        (root / RECORD).write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
        bible = bible.model_copy(update={"characters": [new_specs.get(s.id, s) for s in bible.characters]})
        lock = lock.model_copy(update={"characters": entries})
    return bible, lock, library, changed


def keep_faces(root: Path, lock, library, log=print) -> None:
    """Every run (approved or not): a look's face is the owner's picture. A restore that lost it gets it back from
    the project's looks/; never an AI face in its place (build_identity_pack refuses to draw one)."""
    from .identity import _front_of
    looks = choices(root)
    for cid, r in record(root).items():
        entry = lock.characters.get(cid)
        asset = library.assets.get(_asset_key(r.get("asset_ref") or ""))
        if not r.get("picture") or entry is None or asset is None or entry.character_asset != r.get("asset_ref"):
            continue
        front = _front_of(library, asset)
        look = looks.get(cid) or {}
        name = str(look.get("image") or "")
        src = Path(root) / FOLDER / name
        if front.exists() or look.get("removed") or fingerprint(look) != r.get("fp") or not (
                PICTURE_NAME.fullmatch(name) and src.is_file()):
            continue
        front.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, front)
        log(f"  {cid}: the owner's picture put back as its face")


def _with_build(spec, build: str | None):
    if not build or build == spec.physical.build:
        return spec
    return spec.model_copy(update={"physical": spec.physical.model_copy(update={"build": build})})


def _asset_key(ref: str) -> str:
    """'characters/arjun_x@v1' -> 'character:arjun_x'."""
    return "character:" + ref.rsplit("/", 1)[-1].split("@")[0]
