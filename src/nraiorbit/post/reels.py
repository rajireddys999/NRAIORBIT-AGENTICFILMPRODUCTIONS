"""Reels: short vertical clips cut from the finished film itself, for Instagram Reels and YouTube Shorts.

Nothing is paid for: each reel is a stretch of 11_master/movie.mp4, picture and sound as the film has them (so lips
stay in sync and no new music is needed), set upright at 1080x1920 over a blurred, enlarged copy of itself, with the
film's name at the top, the line spoken in it at the bottom (Telugu over English) and the AI-made tag.

Choosing them (plan_reels, pure): a reel holds one whole line, never a line cut off at either end, over real shots
only (no placeholder), from before the ending (the trailer's SPOILER_CUTOFF); the punchiest lines first, scored as the
trailer scores its punch line, with the picture's own score. Reels sit apart from each other in the film, and when
too few lines fit, a stretch of strong picture with no line cut off makes up the number.

Everything the planner reads is backed up (reports/edl.json, the shot lists, the scenes), so a fresh container makes
them without re-rendering, and they are made again only when the film itself changed (11_master/reels/reels.json).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from ..schemas import SceneScript, Shot, ShotList, StoryBible
from .trailer import SPOILER_CUTOFF, TrailerLine, score_line, score_shot

REEL_S = 5.0                 # each reel's length
REEL_COUNT = 3
REEL_SIZE = (1080, 1920)     # 9:16, as Instagram and YouTube Shorts take them
LEAD_MIN_S, LEAD_MAX_S = 0.3, 1.5    # picture before the line starts
TAIL_MIN_S = 0.3             # and after it ends
APART_S = 8.0                # reels never closer than this in the film
STEP_S = 0.1
TOL_S = 1e-3                 # starts are kept to the millisecond: a line on a frame time is never shut out by it
FOLDER = "reels"             # under 11_master/
RECORD = "reels.json"
AI_TAG = "AI-made film · AI తో రూపొందించిన చిత్రం"
TITLE_AT, LINE_AT = 0.085, 0.675     # where the text starts (share of the height): inside the area Instagram and
#                                      Shorts leave clear of their own buttons and caption (the top tenth and the
#                                      bottom fifth), just above and just below the film's picture
HASHTAGS = ["#Telugu", "#TeluguShortFilm", "#AIFilm", "#Shorts", "#Reels"]


def _crossing(a: float, b: float, lines: list[dict]) -> bool:
    """Whether any line is cut off by the window [a, b]: it starts before a and runs past it, or runs past b."""
    for ln in lines:
        s, e = float(ln["start_s"]), float(ln["start_s"]) + float(ln["duration_s"])
        if (s < a < e) or (s < b < e):
            return True
    return False


def _shots_under(a: float, b: float, shots: list[dict]) -> list[tuple[dict, float]]:
    """(edl shot, seconds of it inside [a, b]) for every shot the window shows."""
    out = []
    for s in shots:
        s0, s1 = float(s["start_s"]), float(s["start_s"]) + float(s["duration_s"])
        seen = min(b, s1) - max(a, s0)
        if seen > 1e-6:
            out.append((s, seen))
    return out


def _picture_score(under: list[tuple[dict, float]], plans: dict[str, Shot], roles: dict[str, str]) -> float:
    total = sum(seen for _, seen in under)
    if not total:
        return 0.0
    return sum(score_shot(plans[s["shot_id"]], roles) * seen for s, seen in under if s["shot_id"] in plans) / total


def plan_reels(edl: dict, plans: dict[str, Shot], lines: list[TrailerLine], roles: dict[str, str],
               count: int = REEL_COUNT, length: float = REEL_S) -> list[dict]:
    """Up to `count` reels of exactly `length` seconds, best first then in film order: {start_s, duration_s, line_id
    (None for picture only), scene_id, score}. `edl`: reports/edl.json; `plans`: shot_id -> Shot; `lines`: the
    film's placed dialogue with what scores it."""
    film = float(edl.get("film_seconds") or 0)
    shots, placed = edl.get("shots") or [], edl.get("dialogue") or []
    if film < length or not shots:
        return []
    first_line = min((float(d["start_s"]) for d in placed), default=0.0)
    lo, hi = max(0.0, first_line - LEAD_MIN_S), SPOILER_CUTOFF * film      # after the title, before the ending
    punch_ids = {b for p in plans.values() if p.punch_dialogue for b in p.block_ids}
    longest = max((ln.duration_s for ln in lines), default=length) or length

    def usable(a: float) -> list[tuple[dict, float]] | None:
        b = a + length
        if a < lo - TOL_S or b > hi + TOL_S or _crossing(a, b, placed):
            return None
        under = _shots_under(a, b, shots)
        if not under or any(not s.get("real", True) for s, _ in under):
            return None                                   # never a placeholder in a reel
        return under

    candidates = []
    for ln in lines:
        if ln.duration_s > length - LEAD_MIN_S - TAIL_MIN_S:
            continue
        lead = min(LEAD_MAX_S, max(LEAD_MIN_S, (length - ln.duration_s) / 2))
        shifts = sorted({round(x * STEP_S, 3) for x in range(int(-LEAD_MAX_S / STEP_S), int(LEAD_MAX_S / STEP_S) + 1)},
                        key=abs)                         # the centred window first, then nearer ones
        for shift in shifts:
            a = round(ln.film_start_s - lead + shift, 3)
            if not (LEAD_MIN_S - TOL_S <= ln.film_start_s - a and
                    a + length - (ln.film_start_s + ln.duration_s) >= TAIL_MIN_S - TOL_S):
                continue
            under = usable(a)
            if under is None:
                continue
            score = 0.6 * score_line("punch", ln, roles, punch_ids, film, longest) + 0.4 * _picture_score(
                under, plans, roles)
            candidates.append({"start_s": a, "line_id": ln.line_id, "score": round(score, 4),
                               "scene_id": max(under, key=lambda u: u[1])[0].get("scene_id")})
            break
    candidates.sort(key=lambda c: -c["score"])
    chosen: list[dict] = []
    for c in candidates:
        if len(chosen) >= count:
            break
        if all(abs(c["start_s"] - k["start_s"]) >= length + APART_S for k in chosen):
            chosen.append(c)
    if len(chosen) < count:                               # strong picture, no line cut off, to make up the number
        pictures = []
        a = lo
        while a + length <= hi + 1e-6:
            under = usable(round(a, 3))
            if under is not None:
                pictures.append({"start_s": round(a, 3), "line_id": None,
                                 "score": round(0.4 * _picture_score(under, plans, roles), 4),
                                 "scene_id": max(under, key=lambda u: u[1])[0].get("scene_id")})
            a += 0.5
        for c in sorted(pictures, key=lambda c: -c["score"]):
            if len(chosen) >= count:
                break
            if all(abs(c["start_s"] - k["start_s"]) >= length + APART_S for k in chosen):
                chosen.append(c)
    return [{**c, "duration_s": length} for c in sorted(chosen, key=lambda c: c["start_s"])]


# ---------------------------------------------------------------------------------------------- the picture

def render_overlay(out: Path, title_te: str, title_en: str, line_te: str = "", line_en: str = "",
                   size: tuple[int, int] = REEL_SIZE) -> Path:
    """A transparent PNG laid over the whole reel: the film's name in a dark band at the top, the line at the bottom
    (Telugu over English) and the AI-made tag. Telugu is drawn only where Pillow can shape it (cards.telugu_font)."""
    from PIL import Image, ImageDraw, ImageFont

    from . import ffmpeg
    from .assemble import TITLE_FONTS
    from .cards import CARD_EDGE, CARD_GOLD, telugu_font
    w, h = size
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for y in range(int(h * 0.24)):                       # the top band fades down into the picture
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, int(190 * (1 - y / (h * 0.24)))))
    for y in range(int(h * 0.64), h):                    # and the bottom one up from the edge
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, int(200 * min(1.0, (y - h * 0.64) / (h * 0.12)))))
    te_file = telugu_font()
    en_file = next((f for f in TITLE_FONTS if Path(f).exists()), None) or ffmpeg.font_file()

    def font(path, px, shaped=False):
        if not path:
            return None
        return ImageFont.truetype(path, px, layout_engine=ImageFont.Layout.RAQM) if shaped else \
            ImageFont.truetype(path, px)

    def centred(text, f, y, fill, edge=None):
        if not text or f is None:
            return y
        lines, words = [], text.split()
        while words:                                     # wrap to the frame, word by word
            take = len(words)
            while take > 1 and f.getlength(" ".join(words[:take])) > w * 0.86:
                take -= 1
            lines.append(" ".join(words[:take]))
            words = words[take:]
        for ln in lines:
            left, top, right, bottom = f.getbbox(ln)
            draw.text(((w - (right - left)) // 2 - left, y - top), ln, font=f, fill=fill,
                      stroke_width=max(2, f.size // 22), stroke_fill=edge or (0, 0, 0, 230))
            y += (bottom - top) + f.size // 3
        return y
    y = int(h * TITLE_AT)
    if te_file:
        y = centred(title_te, font(te_file, 84, shaped=True), y, (*CARD_GOLD, 255), (*CARD_EDGE, 255))
    y = centred(title_en, font(en_file, 46), y, (240, 240, 240, 255))
    y = int(h * LINE_AT)
    if line_te and te_file:
        y = centred(line_te, font(te_file, 58, shaped=True), y, (255, 255, 255, 255))
    if line_en:
        y = centred(line_en, font(en_file, 40), y + 6, (225, 225, 225, 255))
    centred(AI_TAG if te_file else "AI-made film", font(te_file, 30, shaped=True) if te_file else font(en_file, 30),
            y + 24, (210, 210, 210, 235))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def cut_reel(source: Path, start_s: float, length: float, overlay: Path, out: Path, fps: int) -> Path:
    """One reel: [start_s, start_s + length] of the film, picture and sound, upright at 1080x1920 over a blurred,
    enlarged copy of itself, the overlay on top, faded in and out; H.264 high / yuv420p, AAC 48 kHz, faststart."""
    from . import ffmpeg
    w, h = REEL_SIZE
    fade = max(0.0, length - 0.25)
    graph = (f"[0:v]fps={fps},split=2[a][b];"
             f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=28,"
             f"eq=brightness=-0.12[bg];"
             f"[b]scale={w}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[pic];"
             f"[pic][1:v]overlay=0:0:shortest=1,format=yuv420p,"
             f"fade=t=in:st=0:d=0.2,fade=t=out:st={fade:.3f}:d=0.25[v]")
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-ss", f"{start_s:.3f}", "-t", f"{length:.3f}", "-i", str(source), "-loop", "1", "-i", str(overlay),
                "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
                "-af", f"afade=t=in:d=0.15,afade=t=out:st={fade:.3f}:d=0.25",
                "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "19", "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
                "-t", f"{length:.3f}", str(out)])
    return out


def caption(bible: StoryBible, line_te: str = "", line_en: str = "") -> str:
    """A caption ready to paste under the reel: the film, the line, the AI-made note and five hashtags."""
    parts = [f"{bible.title_te} ({bible.title_en})"]
    if line_te or line_en:
        parts.append("\n".join(x for x in (f"“{line_te}”" if line_te else "", line_en) if x))
    parts += [bible.logline, AI_TAG, " ".join(HASHTAGS)]
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------------------------- the film's reels

def _film_mark(movie: Path) -> str:
    h = hashlib.sha256()
    with open(movie, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def make_reels(root: Path, count: int = REEL_COUNT, length: float = REEL_S, fps: int = 24,
               log: Callable[[str], None] = print) -> str:
    """The film's reels in 11_master/reels/ (reel_1.mp4 ...) with reels.json: where each comes from, its line and a
    caption. Made again only when movie.mp4 changed. Returns what happened, for the run report."""
    root = Path(root)
    movie, edl_path = root / "11_master" / "movie.mp4", root / "reports" / "edl.json"
    folder = root / "11_master" / FOLDER
    if not movie.exists() or not edl_path.exists():
        return "skipped: no finished film yet"
    mark = _film_mark(movie)
    try:
        before = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    if before.get("film") == mark and before.get("reels") and \
            all((folder / r["file"]).exists() for r in before["reels"]):
        return f"{len(before['reels'])} up to date"
    bible = StoryBible.model_validate_json((root / "01_story_bible.json").read_text(encoding="utf-8"))
    roles = {c.id: c.role for c in bible.characters}
    plans: dict[str, Shot] = {}
    for p in sorted((root / "04_shots").glob("*.json")):
        plans.update({s.shot_id: s for s in ShotList.model_validate_json(p.read_text(encoding="utf-8")).shots})
    blocks = {}
    for p in sorted((root / "03_scenes").glob("*.json")):
        blocks.update({b.block_id: b for b in SceneScript.model_validate_json(p.read_text(encoding="utf-8")).blocks
                       if b.kind == "dialogue"})
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    lines = [TrailerLine(d["ref_id"], float(d["start_s"]), float(d["duration_s"]), b.character_id,
                         b.delivery.rasa if b.delivery else "shanta", b.delivery.intensity if b.delivery else 5)
             for d in edl.get("dialogue") or [] if (b := blocks.get(d["ref_id"]))]
    picked = plan_reels(edl, plans, lines, roles, count=count, length=length)
    if not picked:
        return "skipped: no stretch of the film fits a reel"
    import shutil
    work = root / "11_master" / f".{FOLDER}.new"         # cut aside: the reels go in whole, or not at all
    shutil.rmtree(work, ignore_errors=True)
    made = []
    try:
        for n, r in enumerate(picked, 1):
            b = blocks.get(r["line_id"]) if r["line_id"] else None
            te, en = (b.text_te, b.text_en) if b else ("", "")
            overlay = render_overlay(work / f"reel_{n}.png", bible.title_te, bible.title_en, te, en)
            out = cut_reel(movie, r["start_s"], r["duration_s"], overlay, work / f"reel_{n}.mp4", fps)
            overlay.unlink(missing_ok=True)
            made.append({"file": out.name, "start_s": r["start_s"], "duration_s": r["duration_s"],
                         "line_id": r["line_id"], "line_te": te, "line_en": en, "scene_id": r["scene_id"],
                         "caption": caption(bible, te, en)})
            log(f"  reel {n}: {r['start_s']:.1f}-{r['start_s'] + r['duration_s']:.1f} s"
                + (f", line {r['line_id']}" if r["line_id"] else ", picture only"))
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        if before.get("film") != mark and folder.exists():   # the old reels are of another cut: none is offered
            (folder / RECORD).write_text(json.dumps({"film": None, "reels": []}), encoding="utf-8")
        raise
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("reel_*.mp4"):
        old.unlink()
    for new in work.glob("reel_*.mp4"):
        new.replace(folder / new.name)
    shutil.rmtree(work, ignore_errors=True)
    (folder / RECORD).write_text(json.dumps({"film": mark, "reels": made}, indent=1, ensure_ascii=False),
                                 encoding="utf-8")
    return f"{len(made)} made"
