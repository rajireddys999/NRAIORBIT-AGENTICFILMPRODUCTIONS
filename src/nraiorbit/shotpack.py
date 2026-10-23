"""Shot pack: everything needed to generate each shot by hand in Veo (Gemini app, Flow or AI Studio).

`nraiorbit shotpack export <slug>` writes, per shot, a ready-to-paste Veo prompt, the
clip length to pick, and the character reference images to attach. Generated clips go
into projects/<slug>/inbox/video/ as <shot_id>.mp4 (best attempt) and <shot_id>_2.mp4,
<shot_id>_3.mp4 ... (other attempts, so the bill estimate counts them).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .costing import VEO_TIER_NAMES, billed_seconds, veo_durations, veo_settings
from .library.registry import AssetRegistry
from .schemas import CastLock, Outline, SceneOutline, Shot, ShotList, StoryBible

SIZE_WORDS = {
    "EWS": "Extreme wide shot", "WS": "Wide shot", "MS": "Medium shot", "MCU": "Medium close-up",
    "CU": "Close-up", "ECU": "Extreme close-up", "OTS": "Over-the-shoulder shot", "POV": "Point-of-view shot",
    "two_shot": "Two-shot", "insert": "Insert detail shot", "aerial": "Aerial shot",
}
MOVE_WORDS = {
    "static": "locked-off camera", "push_in": "slow push-in", "pull_out": "slow pull-out", "pan": "slow pan",
    "tilt": "slow tilt", "track": "tracking shot", "orbit": "360-degree orbit around the subject",
    "crane_up": "crane rising up", "crane_down": "crane moving down", "handheld": "handheld camera",
    "crash_zoom": "fast crash zoom", "dolly_zoom": "dolly zoom", "whip_pan": "whip pan", "drone": "drone shot",
}
FPS_WORDS = {"normal": "", "slowmo": "in slow motion", "speed_ramp": "with a speed ramp into slow motion on impact"}
NEGATIVE = ("text, subtitles, captions, watermark, logo, cartoon, anime, CGI look, extra fingers, "
            "deformed hands, distorted face, blurry face")
CLIP_RE = r"^{shot}(?:_(\d+))?\.(mp4|mov|webm)$"


def selected(shot: Shot, scene_id: str, only: list[str] | None) -> bool:
    """`render.only`: empty = every shot; otherwise scene ids, shot ids, and/or "trailer"."""
    if not only:
        return True
    if "trailer" in only and (shot.trailer_candidate or shot.punch_dialogue):
        return True
    return scene_id in only or shot.shot_id in only


def inbox_dir(root: Path, kind: str) -> Path:
    return root / "inbox" / kind


def clip_for_take(root: Path, shot_id: str, take: int) -> Path | None:
    """The clip of a shot's take: the take's own file (<shot>.mp4 for take 1, <shot>_<take>.mp4 after), which is
    where the video step saves it however the take numbers skip, else the take-th attempt; None when there is none."""
    folder = inbox_dir(root, "video")
    name = shot_id if take == 1 else f"{shot_id}_{take}"
    for ext in ("mp4", "mov", "webm"):
        if (folder / f"{name}.{ext}").exists():
            return folder / f"{name}.{ext}"
    paid = clips_for(root, shot_id)
    return paid[take - 1] if len(paid) >= take else None


def clips_for(root: Path, shot_id: str) -> list[Path]:
    """Attempts for a shot, best first: <shot>.mp4, then <shot>_2.mp4, <shot>_3.mp4 ..."""
    folder = inbox_dir(root, "video")
    if not folder.exists():
        return []
    pattern = re.compile(CLIP_RE.format(shot=re.escape(shot_id)))
    found = []
    for path in folder.iterdir():
        m = pattern.match(path.name)
        if m:
            found.append((int(m.group(1) or 1), path))
    return [p for _, p in sorted(found)]


def _names(text: str, bible: StoryBible) -> str:
    for c in bible.characters:
        text = re.sub(rf"\b{re.escape(c.id)}\b", c.name_en, text)
    return text


def character_line(cid: str, costume_id: str | None, bible: StoryBible, lock: CastLock) -> str:
    spec = next(c for c in bible.characters if c.id == cid)
    ph = spec.physical
    costume = lock.characters[cid].costumes.get(costume_id or "", spec.costume_baseline) if cid in lock.characters \
        else spec.costume_baseline
    marks = "" if ph.marks.lower() == "none" else f", {ph.marks}"
    return (f"{spec.name_en}: {ph.age}-year-old Telugu {ph.gender}, {ph.build}, {ph.skin_tone} skin, {ph.face}, "
            f"{ph.hair}{marks}; wearing {costume}")


def veo_prompt(shot: Shot, scene: SceneOutline, bible: StoryBible, lock: CastLock, navarasa: dict,
               speaking: str | None) -> str:
    look = navarasa["rasas"][shot.rasa]["look"]
    move = MOVE_WORDS.get(shot.camera_move, shot.camera_move)
    camera = f"{SIZE_WORDS.get(shot.size, shot.size)}, {shot.lens_mm}mm lens, {move}"
    fps = FPS_WORDS.get(shot.fps_effect, "")
    parts = [
        f"{camera}{', ' + fps if fps else ''}.",
        _names(shot.image_prompt, bible).rstrip(".") + ".",
        _names(shot.motion_prompt, bible).rstrip(".").capitalize() + ".",
    ]
    if shot.characters_in_frame:
        people = "; ".join(character_line(c, shot.costume_ids[i] if i < len(shot.costume_ids) else None, bible, lock)
                           for i, c in enumerate(shot.characters_in_frame))
        parts.append(f"Characters (match the reference images exactly): {people}.")
    if speaking:   # lip sync later redraws the mouth to the Telugu line; the face must be clearly visible for it
        parts.append(f"{speaking} is speaking, face clearly visible to camera, mouth moving naturally; "
                     "no audible dialogue needed. Everyone else keeps their mouth closed.")
    elif shot.characters_in_frame:
        parts.append("Nobody is talking: mouths closed and still, no lip movement.")
    atmosphere = ", ".join(look.get("atmosphere", []))
    parts.append(f"Setting: {scene.heading.title()}, {bible.era_and_region}.")
    parts.append(f"Look: {look['palette'].replace('_', ' ')} palette, {look['contrast'].replace('_', ' ')} contrast"
                 f"{', ' + atmosphere.replace('_', ' ') if atmosphere else ''}. "
                 "Photorealistic cinematic Telugu feature film, natural skin texture, 24fps film look, 16:9.")
    return " ".join(parts)


def _reference_images(cid: str, costume_id: str | None, lock: CastLock, library: AssetRegistry) -> list[Path]:
    if cid not in lock.characters or lock.characters[cid].character_asset.endswith("@pending"):
        return []
    asset = library.resolve(lock.characters[cid].character_asset)
    base = library.manifest_dirs[f"character:{asset.id}"]
    images: list[Path] = []
    face = base / asset.files.get("face_refs", "refs/face/")
    if face.is_dir():
        images += sorted(p for p in face.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))[:2]
    costume = next((c for c in getattr(asset, "costumes", []) if c.id == costume_id), None)
    if costume and costume.refs and (base / costume.refs).is_dir():
        images += sorted(p for p in (base / costume.refs).iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))[:1]
    return images


def export(root: Path, library: AssetRegistry, navarasa: dict, pricing: dict, google: dict | None = None) -> Path:
    """google: the project's google: settings (default Veo resolution and the video_tiers shots pick)."""
    import yaml

    bible = StoryBible.model_validate_json((root / "01_story_bible.json").read_text(encoding="utf-8"))
    outline = Outline.model_validate_json((root / "02_outline.json").read_text(encoding="utf-8"))
    lock = CastLock.model_validate(yaml.safe_load((root / "cast.lock.yaml").read_text(encoding="utf-8")))
    ext = pricing["veo"]["extension_s"]
    pack = root / "shotpack"
    if pack.exists():
        shutil.rmtree(pack)
    inbox_dir(root, "video").mkdir(parents=True, exist_ok=True)
    names = {c.id: c.name_en for c in bible.characters}

    index = [f"# Shot pack: {bible.title_en}", "",
             "For each shot: paste the prompt into Veo, set the clip length, attach the reference images "
             "(as ingredients / reference images), generate, and save the clip into `inbox/video/` with the name "
             "shown. Save extra attempts as `<shot>_2.mp4`, `<shot>_3.mp4` so the bill counts them.", "",
             f"Negative prompt for every shot: {NEGATIVE}", ""]
    records = []
    missing_refs: set[str] = set()
    for sc in outline.scenes:
        script_blocks = {}
        script_path = root / "03_scenes" / f"{sc.scene_id}.json"
        if script_path.exists():
            script_blocks = {b["block_id"]: b for b in json.loads(script_path.read_text(encoding="utf-8"))["blocks"]}
        shots = ShotList.model_validate_json((root / "04_shots" / f"{sc.scene_id}.json").read_text(encoding="utf-8"))
        index += [f"## {sc.scene_id}: {sc.heading}", ""]
        for shot in shots.shots:
            speaker = next((script_blocks[b]["character_id"] for b in shot.block_ids
                            if b in script_blocks and script_blocks[b]["kind"] == "dialogue"
                            and script_blocks[b]["character_id"] in shot.characters_in_frame), None)
            prompt = veo_prompt(shot, sc, bible, lock, navarasa, names.get(speaker) if speaker else None)
            _, tier_name, resolution = veo_settings(google or {}, shot)   # 1080p clips are 8 s only
            tier = f"{VEO_TIER_NAMES.get(tier_name, tier_name)} {resolution}" if shot.video_tier else ""
            clip_s, n_clips = billed_seconds(shot.duration_s, veo_durations(pricing, resolution), ext)
            folder = pack / shot.shot_id
            (folder / "refs").mkdir(parents=True)
            refs = []
            for i, cid in enumerate(shot.characters_in_frame):
                costume = shot.costume_ids[i] if i < len(shot.costume_ids) else None
                imgs = _reference_images(cid, costume, lock, library)
                if not imgs:
                    missing_refs.add(cid)
                for img in imgs:
                    dest = folder / "refs" / f"{cid}_{img.name}"
                    shutil.copy2(img, dest)
                    refs.append(dest.relative_to(pack).as_posix())
            length = f"{clip_s} s" if n_clips == 1 else f"8 s + {n_clips - 1} extension(s) of {ext} s"
            (folder / "prompt.txt").write_text(
                f"{prompt}\n\nNegative: {NEGATIVE}\nClip length: {length} (shot uses {shot.duration_s:g} s)\n"
                + (f"Model: {tier}\n" if tier else "") + f"Save as: inbox/video/{shot.shot_id}.mp4\n",
                encoding="utf-8")
            who = ", ".join(names.get(c, c) for c in shot.characters_in_frame) or "no lead characters"
            index += [f"### {shot.shot_id} · {length}{' · ' + tier if tier else ''} · {who}",
                      "", prompt, "", f"References: {', '.join(refs) if refs else 'none'}", ""]
            records.append({"shot_id": shot.shot_id, "scene_id": sc.scene_id, "prompt": prompt, "negative": NEGATIVE,
                            "clip_seconds": clip_s, "clips": n_clips, "shot_seconds": shot.duration_s, "refs": refs,
                            "video_tier": shot.video_tier})
    if missing_refs:
        index[4:4] = [f"**Reference images missing** for: {', '.join(sorted(missing_refs))}. Add face and costume "
                      "images to their library folders (library/characters/<id>/refs/) and export again; without them "
                      "Veo will invent a different face in every clip.", ""]
    (pack / "SHOTPACK.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    _write_dialogue_sheet(root, pack, bible, outline)
    _write_music_sheet(root, pack)
    (pack / "shots.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return pack


def status(root: Path) -> dict:
    outline = Outline.model_validate_json((root / "02_outline.json").read_text(encoding="utf-8"))
    done, missing, attempts = [], [], 0
    for sc in outline.scenes:
        for shot in ShotList.model_validate_json((root / "04_shots" / f"{sc.scene_id}.json").read_text(encoding="utf-8")).shots:
            clips = clips_for(root, shot.shot_id)
            attempts += len(clips)
            (done if clips else missing).append(shot.shot_id)
    return {"done": done, "missing": missing, "attempts": attempts}


def _write_dialogue_sheet(root: Path, pack: Path, bible: StoryBible, outline: Outline) -> None:
    """Recording script: save each take as inbox/dialogue/<line id>.wav."""
    names = {c.id: f"{c.name_en} ({c.name_te})" for c in bible.characters}
    out = [f"# Dialogue recording sheet: {bible.title_en}", "",
           "Record each line (or generate it with Telugu TTS) and save it as `inbox/dialogue/<line id>.wav`. "
           "Lines without a file use the configured fallback voice.", ""]
    for sc in outline.scenes:
        blocks = json.loads((root / "03_scenes" / f"{sc.scene_id}.json").read_text(encoding="utf-8"))["blocks"]
        lines = [b for b in blocks if b["kind"] == "dialogue"]
        if not lines:
            continue
        out += [f"## {sc.scene_id}: {sc.heading}", ""]
        for b in lines:
            d = b.get("delivery") or {}
            out += [f"**{b['block_id']}** · {names.get(b['character_id'], b['character_id'])} · "
                    f"{d.get('rasa', '')} {d.get('intensity', '')}/10, {d.get('pace', '')}, {d.get('volume', '')}",
                    "", f"> {b['text_te']}", "", f"_{b['text_en']}_ · {d.get('notes', '')}", ""]
    (pack / "DIALOGUE.md").write_text("\n".join(out) + "\n", encoding="utf-8")


def _write_music_sheet(root: Path, pack: Path) -> None:
    sheet_path = root / "08_music" / "cue_sheet.json"
    if not sheet_path.exists():
        return
    cues = json.loads(sheet_path.read_text(encoding="utf-8"))["cues"]
    out = ["# Music cue sheet", "",
           "Compose, commission or license each cue and save it as `inbox/music/<cue id>.wav`. "
           "Cues are trimmed or padded to length and faded out; missing cues play a placeholder tone.", "",
           "| Cue | Starts | Length | Scenes | Emotion | Raga / tala / BPM | Instruments | Theme | Peak |",
           "|---|---|---|---|---|---|---|---|---|"]
    for c in cues:
        start = f"{int(c['start_s'] // 60)}:{int(c['start_s'] % 60):02d}"
        out.append(f"| {c['cue_id']} | {start} | {c['duration_s']:.0f} s | {', '.join(c['scene_ids'])} | "
                   f"{c['rasa']} {c['intensity']}/10 | {c['raga']} / {c['tala']} / {c['bpm']} | "
                   f"{', '.join(c['instruments'])} | {c['leitmotif_character'] or '-'} | "
                   f"{'yes, ' + str(c['silence_before_s']) + ' s silence before' if c['peak'] else ''} |")
    (pack / "MUSIC.md").write_text("\n".join(out) + "\n", encoding="utf-8")
