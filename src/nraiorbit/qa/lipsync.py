"""Frame-level lip-sync QA for every speaking shot of a project (`nraiorbit qa lipsync <slug>`).

For each shot in reports/lipsync.jsonl (the latest row per shot): the speaker's mouth aperture from the face mesh on
the filmed take and on the composited shot, against the line's loudness per frame; closed-mouth runs while the voice
plays; mouth motion in silence; the largest change outside the speaker's face (a blob or smear, as opposed to codec
noise); a frame strip of the Veo crop against the raw lip-sync result with loudness bars; a composite sheet at
speech onset, the loudest frames and silence. Everything goes under reports/qa/lipsync/ with a summary.json and a
summary.md that names the shots worth a human look. Nothing here spends money.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

FPS = 24
SPEECH_FLOOR_DB = -40.0       # a frame is speech when its loudness is above max(floor, peak - 28 dB)
SPEECH_BELOW_PEAK_DB = 28.0
CLOSED_RUN_FRAMES = 6         # a closed mouth for this many frames of speech (0.25 s) is worth a look
BLOB_BLOCK = 24               # pixels; a block this size whose mean change exceeds BLOB_LEVEL is a blob
BLOB_LEVEL = 10.0             # codec noise sits at 1-3; real smears are 10+


# ---------------------------------------------------------------------------------------------------- pure parts

def envelope_db(wav: Path, frames: int, fps: int = FPS, rate: int = 16000) -> np.ndarray:
    """Loudness per video frame (dBFS of the RMS), -120 where the frame is past the end of the audio."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    hop = rate // fps
    rms = np.array([np.sqrt((a[i * hop:(i + 1) * hop] ** 2).mean()) if (i + 1) * hop <= len(a) else 0.0
                    for i in range(frames)])
    return 20 * np.log10(rms + 1e-6)


def speech_mask(env: np.ndarray) -> np.ndarray:
    peak = float(np.nanmax(env)) if len(env) else -120.0
    return env > max(SPEECH_FLOOR_DB, peak - SPEECH_BELOW_PEAK_DB)


def aperture(points: list, up: int, lo: int, left_eye: int, right_eye: int) -> np.ndarray:
    """Inner-lip gap over the distance between the eyes' outer corners, per frame; nan where there is no mesh."""
    out = np.full(len(points), np.nan)
    for i, p in enumerate(points):
        if p is not None:
            out[i] = np.hypot(*(p[up] - p[lo])) / max(np.hypot(*(p[left_eye] - p[right_eye])), 1e-3)
    return out


def runs(mask, least: int) -> list[tuple[int, int]]:
    """(first, last) of every run of True at least `least` long."""
    res, start = [], None
    for i, m in enumerate([*list(mask), False]):
        if m and start is None:
            start = i
        if not m and start is not None:
            if i - start >= least:
                res.append((start, i - 1))
            start = None
    return res


def corr_lag(ap: np.ndarray, env: np.ndarray, speech: np.ndarray, lags=range(-12, 13)) -> tuple[float, int]:
    """Best correlation between mouth aperture and loudness over the speech frames, and the lag (frames) that
    gives it: positive means the mouth follows the sound. (nan, 0) when there is too little to measure."""
    best = (float("nan"), 0)
    for lag in lags:
        a = np.roll(ap, -lag)
        ok = speech & ~np.isnan(a) & ~np.isnan(env)
        if ok.sum() < 8 or np.std(a[ok]) < 1e-9 or np.std(env[ok]) < 1e-9:
            continue
        c = float(np.corrcoef(a[ok], env[ok])[0, 1])
        if np.isnan(best[0]) or c > best[0]:
            best = (c, lag)
    return best


def block_diff(a: np.ndarray, b: np.ndarray, box=None, block: int = BLOB_BLOCK, grow: float = 0.5) -> float:
    """The largest mean absolute change between two frames in any block x block square outside `box` (x0, y0, x1,
    y1, grown by `grow` of its size on every side). Codec noise gives 1-3; a pasted blob gives 10 and more."""
    d = np.abs(b.astype(np.int16) - a.astype(np.int16))
    d = d.mean(2) if d.ndim == 3 else d
    if box is not None:
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        d[int(max(0, y0 - grow * h)):int(y1 + grow * h), int(max(0, x0 - grow * w)):int(x1 + grow * w)] = 0
    H, W = d.shape
    if H < block or W < block:
        return float(d.max()) if d.size else 0.0
    d = d[:H // block * block, :W // block * block].reshape(H // block, block, W // block, block).mean((1, 3))
    return float(d.max())


def review_picks(env: np.ndarray, speech: np.ndarray, n: int) -> list[int]:
    """Six frames worth looking at: speech onset (+0, +3), the two loudest, one silent frame before and one after."""
    sp = np.flatnonzero(speech)
    if not len(sp):
        return list(range(0, n, max(1, n // 6)))[:6]
    picks = [int(sp[0]), int(min(n - 1, sp[0] + 3))]
    for f in sp[np.argsort(env[sp])[::-1]]:
        if all(abs(int(f) - q) > 4 for q in picks):
            picks.append(int(f))
        if len(picks) >= 4:
            break
    sil = np.flatnonzero(~speech)
    before, after = sil[sil < sp[0]], sil[sil > sp[-1]]
    if len(before):
        picks.append(int(before[len(before) // 2]))
    if len(after):
        picks.append(int(after[min(len(after) - 1, 6)]))
    return sorted(set(picks))[:6]


def verdict(row: dict) -> list[str]:
    """Plain-language flags from one shot's measurements; an empty list means nothing stood out."""
    flags = []
    if row.get("blob_max", 0) > BLOB_LEVEL:
        flags.append(f"change outside the face ({row['blob_max']:.0f}) in {row.get('blob_frames', 0)} frames")
    long_runs = [(a, b) for a, b in row.get("closed_runs", []) if b - a + 1 >= 3 * CLOSED_RUN_FRAMES]
    if long_runs:
        flags.append("mouth closed for " + ", ".join(f"f{a}-{b}" for a, b in long_runs) + " while the voice plays")
    if (row.get("mesh_missing") or 0) > 0.25 * max(1, row.get("frames", 1)):
        flags.append(f"no face mesh in {row['mesh_missing']} frames (turned or hidden face)")
    if row.get("failed_segments"):
        flags.append("a segment was not lip-synced")
    return flags


# --------------------------------------------------------------------------------------------- the review itself

def latest_rows(project_root: Path, shots=None) -> dict[str, dict]:
    rows = {}
    path = project_root / "reports" / "lipsync.jsonl"
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if not shots or r["shot_id"] in shots:
                rows[r["shot_id"]] = r
    return rows


def review(project_root: Path, out_dir: Path | None = None, shots=None, model_dir: Path | None = None,
           strips: bool = True, log=print) -> list[dict]:
    """Measure every (latest) lip-synced shot and write the sheets; returns the per-shot rows."""
    from PIL import Image, ImageDraw

    from ..post import facelock as fl

    project_root = Path(project_root)
    out_dir = Path(out_dir or project_root / "reports" / "qa" / "lipsync")
    out_dir.mkdir(parents=True, exist_ok=True)
    det, mesher = fl.BlazeFace(model_dir), fl.FaceMesh(model_dir)
    mp = list(fl.MESH_POINTS)
    up, lo, le, re_ = (mp.index(k) for k in (13, 14, 33, 263))
    cache = project_root / "cache" / "faces"
    out_rows = []
    for sid, rec in sorted(latest_rows(project_root, shots).items()):
        clip = Path(rec["clip"])
        if not clip.is_absolute():
            clip = (project_root.parent.parent / clip) if not clip.exists() else clip
        synced, wav = clip.with_name(f"{clip.stem}_lipsync.mp4"), clip.with_name(f"{clip.stem}.lipsync.wav")
        if not synced.exists() or not wav.exists():
            out_rows.append({"shot": sid, "mode": rec.get("mode"), "flags": ["no composited clip to check"]})
            continue
        an = fl.analyse(clip, det, cache_dir=cache)
        track = fl.pick_speaker(an, hint=((rec.get("speaker") or {}).get("hint")))
        faces = fl.fill_gaps(track, an.frames) if track else [None] * an.frames
        n = an.frames
        pts_o = fl.meshes(clip, faces, mesher, cache_dir=cache)
        pts_s = fl.meshes(synced, faces, mesher, cache_dir=cache)
        env = envelope_db(wav, n)
        speech = speech_mask(env)
        ap_o, ap_s = aperture(pts_o, up, lo, le, re_), aperture(pts_s, up, lo, le, re_)
        silent = ~speech & ~np.isnan(ap_s)
        closed_level = (np.nanmedian(ap_s[silent]) if silent.sum() >= 4 else np.nanmin(ap_s)) + 0.012
        c_s, lag_s = corr_lag(ap_s, env, speech)
        c_o, _ = corr_lag(ap_o, env, speech)
        picks = review_picks(env, speech, n)
        blob_max, blob_frames, tiles = 0.0, 0, []
        for i, (fo, fs) in enumerate(zip(fl.read_frames(clip), fl.read_frames(synced))):
            f = faces[i] if i < len(faces) else None
            bd = block_diff(fo, fs, f.box if f is not None else None)
            blob_max = max(blob_max, bd)
            blob_frames += bd > BLOB_LEVEL
            if i in picks:
                tiles.append(_tile(Image, ImageDraw, sid, i, fo, fs, pts_s[i] if pts_s[i] is not None else pts_o[i],
                                   f, env[i], bool(speech[i]), ap_o[i], ap_s[i], up, lo))
        if tiles:
            _stack(Image, tiles).save(out_dir / f"{sid}_composite.jpg", quality=88)
        row = {"shot": sid, "mode": rec.get("mode"), "frames": n, "speech_frames": int(speech.sum()),
               "corr_synced": None if np.isnan(c_s) else round(c_s, 2), "lag_synced": int(lag_s),
               "corr_filmed": None if np.isnan(c_o) else round(c_o, 2),
               "aperture_speech": _r(np.nanmean(ap_s[speech])) if speech.any() else None,
               "aperture_speech_filmed": _r(np.nanmean(ap_o[speech])) if speech.any() else None,
               "aperture_silent": _r(np.nanmean(ap_s[silent])) if silent.any() else None,
               "closed_runs": [[int(a), int(b)] for a, b in runs(speech & (ap_s < closed_level), CLOSED_RUN_FRAMES)],
               "silent_motion": _r(np.nanstd(ap_s[silent])) if silent.sum() > 2 else None,
               "blob_max": round(blob_max, 1), "blob_frames": int(blob_frames),
               "mesh_missing": int(sum(p is None for p in pts_s)), "failed_segments": rec.get("failed_segments"),
               "picks": picks}
        row["flags"] = verdict(row)
        out_rows.append(row)
        log(f"  {sid}: {'; '.join(row['flags']) or 'nothing stood out'}")
        if strips:
            try:
                strip(project_root, sid, rec, an, faces, out_dir / f"{sid}_strip.jpg", fl, Image, ImageDraw)
            except Exception as exc:                        # a strip is help for the eye, never a reason to stop
                log(f"  {sid}: no strip ({type(exc).__name__}: {exc})")
    (out_dir / "summary.json").write_text(json.dumps(out_rows, indent=1), encoding="utf-8")
    flagged = [r for r in out_rows if r.get("flags")]
    md = ["# Lip-sync QA", "", f"{len(out_rows)} shots measured, {len(flagged)} worth a look.", ""]
    for r in flagged:
        md.append(f"- **{r['shot']}**: " + "; ".join(r["flags"]) + f" (see {r['shot']}_composite.jpg)")
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out_rows


def _r(x) -> float | None:
    return None if x is None or np.isnan(x) else round(float(x), 3)


def _tile(Image, ImageDraw, sid, i, fo, fs, p, face, db, speech, ap_o, ap_s, up, lo):
    if p is not None:
        cx, cy = p[[up, lo]].mean(0)
        s = max(float(np.hypot(*(p[up] - p[lo]))) * 6, 90)
    elif face is not None:
        x0, y0, x1, y1 = face.box
        cx, cy, s = (x0 + x1) / 2, y0 + .75 * (y1 - y0), max(x1 - x0, 90) * .9
    else:
        cx, cy, s = fo.shape[1] / 2, fo.shape[0] / 2, 240
    bx0, by0 = int(max(0, cx - s)), int(max(0, cy - s * .75))
    bx1, by1 = int(min(fo.shape[1], cx + s)), int(min(fo.shape[0], cy + s * .75))
    o, sN = fo[by0:by1, bx0:bx1], fs[by0:by1, bx0:bx1]
    d = np.clip(np.abs(sN.astype(np.int16) - o.astype(np.int16)).mean(2) * 4, 0, 255).astype(np.uint8)
    panels = [Image.fromarray(o), Image.fromarray(sN), Image.fromarray(np.stack([d, d // 2, d // 4], -1))]
    panels = [pn.resize((320, int(320 * pn.height / max(pn.width, 1))), Image.LANCZOS) for pn in panels]
    w, h = panels[0].size
    t = Image.new("RGB", (w * 3 + 12, h + 18), (20, 20, 20))
    for k, pn in enumerate(panels):
        t.paste(pn, (k * (w + 6), 18))
    ImageDraw.Draw(t).text((3, 2), f"{sid} f{i} t={i / FPS:.2f}s {db:.0f} dB {'SPEECH' if speech else 'silent'} "
                                   f"aperture filmed {ap_o:.3f} synced {ap_s:.3f}  (filmed | synced | change x4)",
                           fill=(255, 220, 0))
    return t


def _stack(Image, tiles):
    w, h = tiles[0].size
    sheet = Image.new("RGB", (w, h * len(tiles)), (0, 0, 0))
    for k, t in enumerate(tiles):
        sheet.paste(t, (0, k * h))
    return sheet


def strip(project_root: Path, sid: str, rec: dict, an, faces, out: Path, fl, Image, ImageDraw, step: int = 2,
          tw: int = 96) -> None:
    """Every `step`-th frame of each speaker crop (top) and its raw lip-sync result (bottom), mouth close-ups, with
    the crop's loudness bars (red = speech). Needs the crops and the provider's cached results."""
    clip = Path(rec["clip"])
    work, stem = clip.parent, clip.stem
    from ..providers import sync_so  # noqa: F401  (registered providers may be needed for cache keys)
    segs = fl.plan_segments(faces, an.width, an.height, set())
    blocks_all = []
    th = int(tw * 0.8)
    for k, seg in enumerate(segs):
        crop, wav = work / f"{stem}_crop{k}.mp4", work / f"{stem}_crop{k}.wav"
        res = work / f"{stem}_crop{k}_synced.mp4"
        if not (crop.exists() and wav.exists() and res.exists()):
            continue
        db = envelope_db(wav, seg.end - seg.start)
        frames_a, frames_b = list(fl.read_frames(crop)), list(fl.read_frames(res))
        x, y, _, _ = seg.window
        rows = []
        for i in range(0, min(len(frames_a), len(frames_b)), step):
            f = faces[seg.start + i] if seg.start + i < len(faces) else None
            if f is None:
                cx, cy, s = frames_a[0].shape[1] / 2, frames_a[0].shape[0] * 0.6, 120
            else:
                cx, cy = (f.box[0] + f.box[2]) / 2 - x, f.box[1] - y + 0.78 * (f.box[3] - f.box[1])
                s = max((f.box[2] - f.box[0]) * 0.45, 60)
            bx0, by0 = int(max(0, cx - s)), int(max(0, cy - s * 0.8))
            bx1, by1 = int(min(frames_a[0].shape[1], cx + s)), int(min(frames_a[0].shape[0], cy + s * 0.8))
            rows.append((i, Image.fromarray(frames_a[i][by0:by1, bx0:bx1]).resize((tw, th)),
                         Image.fromarray(frames_b[i][by0:by1, bx0:bx1]).resize((tw, th))))
        per = 15
        for n in range(0, len(rows), per):
            chunk = rows[n:n + per]
            im = Image.new("RGB", (tw * per, th * 2 + 40), (15, 15, 15))
            d = ImageDraw.Draw(im)
            for j, (i, o, r) in enumerate(chunk):
                im.paste(o, (j * tw, 12))
                im.paste(r, (j * tw, 12 + th))
                d.text((j * tw + 2, 0), f"f{seg.start + i}", fill=(255, 220, 0))
                for q in range(step):
                    if i + q < len(db):
                        hgt = int(max(0, db[i + q] + 50) / 50 * 24)
                        col = (255, 80, 80) if db[i + q] > -32 else (90, 90, 90)
                        d.rectangle([j * tw + q * (tw // step) + 1, th * 2 + 38 - hgt,
                                     j * tw + (q + 1) * (tw // step) - 2, th * 2 + 38], fill=col)
            blocks_all.append(im)
    if not blocks_all:
        return
    sheet = Image.new("RGB", (blocks_all[0].width, sum(b.height for b in blocks_all) + 14), (0, 0, 0))
    ImageDraw.Draw(sheet).text((2, 1), f"{sid}: top = filmed crop, bottom = lip-sync result, bars = loudness "
                                       "(red = speech)", fill=(255, 220, 0))
    yy = 14
    for b in blocks_all:
        sheet.paste(b, (0, yy))
        yy += b.height
    sheet.save(out, quality=88)
