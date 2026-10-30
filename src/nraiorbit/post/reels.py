"""Reels: short vertical clips cut from the finished film itself, for Instagram Reels and YouTube Shorts.

Nothing is paid for: each reel is a stretch of 11_master/movie.mp4, picture and sound as the film has them (so lips
stay in sync and no new music is needed), set upright at 1080x1920 over a blurred, enlarged copy of itself, with the
film's name at the top, the line spoken in it at the bottom (Telugu over English) and the AI-made tag.

Choosing them (plan_reels, pure): each reel is a whole moment, 5 to 15 seconds from one cut to a later cut of the
same scene, so it starts and ends where the film itself cuts and no line is cut off at either end; real shots only
(no placeholder), from before the ending (the trailer's SPOILER_CUTOFF). The punchiest first, scored as the trailer
scores its punch line, with the picture's own score; one from each scene before a second from any, never overlapping.
How many: the owner's number for the film (service/shorts.count), else the config's.

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

SHORTEST_S, LONGEST_S = 5.0, 15.0    # each reel: a whole moment, cut to cut
REEL_COUNT = 3
MAX_COUNT = 20               # the most the owner can ask for in one film
GAP_S = 0.5                  # reels never overlap (nor quite touch) in the film
PICTURE_ONLY = 0.75          # a moment with no line counts for less than one with a line
FADE_IN_S, FADE_OUT_S = 0.15, 0.3    # a reel fades in and out: a line inside keeps clear of both
CARD_CLEAR_S = 0.3           # and a reel keeps this clear of a card laid over the picture (the title, INTERVAL)
PLAN = 2                     # how reels are chosen: a reel set made another way is made again
REEL_SIZE = (1080, 1920)     # 9:16, as Instagram and YouTube Shorts take them
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
               count: int = REEL_COUNT, shortest: float = SHORTEST_S, longest: float = LONGEST_S,
               cards: list[tuple[float, float]] = ()) -> list[dict]:
    """Up to `count` reels, each a whole moment: from one cut to a later cut of the same scene, `shortest` to
    `longest` seconds, every line in it whole (none cut off at either end), real shots only, before the ending.
    The strongest first (one from each scene before a second from any), then in film order: {start_s, duration_s,
    line_id (its strongest line; None for picture only), scene_id, score, shots}. `edl`: reports/edl.json; `plans`:
    shot_id -> Shot; `lines`: the film's placed dialogue with what scores it; `cards`: (start, end) of the cards laid
    over the film's picture (card_spans), which no reel shows."""
    film = float(edl.get("film_seconds") or 0)
    shots = sorted(edl.get("shots") or [], key=lambda s: float(s["start_s"]))
    placed = edl.get("dialogue") or []
    if not shots or count < 1:
        return []
    hi = SPOILER_CUTOFF * film                            # never the ending
    punch_ids = {b for p in plans.values() if p.punch_dialogue for b in p.block_ids}
    strongest = max((ln.duration_s for ln in lines), default=longest) or longest
    candidates = []
    for i, first in enumerate(shots):
        a = float(first["start_s"])
        for last in shots[i:]:                            # one cut to a later one, inside the scene
            if last.get("scene_id") != first.get("scene_id") or not last.get("real", True) \
                    or not first.get("real", True):
                break                                     # never a placeholder in a reel
            b = float(last["start_s"]) + float(last["duration_s"])
            if b - a > longest + TOL_S or b > hi + TOL_S:
                break
            if any(a < e + CARD_CLEAR_S and s - CARD_CLEAR_S < b for s, e in cards):
                continue                                  # never a card over the picture
            if b - a < shortest - TOL_S or _crossing(a, b, placed) or any(
                    a < float(d["start_s"]) + float(d["duration_s"]) and float(d["start_s"]) < b and (
                        float(d["start_s"]) < a + FADE_IN_S - TOL_S or
                        float(d["start_s"]) + float(d["duration_s"]) > b - FADE_OUT_S + TOL_S) for d in placed):
                continue                                  # a line cut off, or under the reel's own fades
            under = _shots_under(a, b, shots)
            picture = _picture_score(under, plans, roles)
            inside = [ln for ln in lines if ln.film_start_s >= a - TOL_S
                      and ln.film_start_s + ln.duration_s <= b + TOL_S]
            said = {ln.line_id: score_line("punch", ln, roles, punch_ids, film, strongest) for ln in inside}
            best = max(said, key=said.get) if said else None
            score = 0.6 * said[best] + 0.4 * picture if best else PICTURE_ONLY * 0.4 * picture
            candidates.append({"start_s": round(a, 3), "duration_s": round(b - a, 3), "line_id": best,
                               "score": round(score, 4), "scene_id": first.get("scene_id"),
                               "shots": [s["shot_id"] for s, _ in under]})
    candidates.sort(key=lambda c: (-c["score"], c["duration_s"], c["start_s"]))   # the shorter of equals: room
    #                                                                                  for as many as asked
    chosen: list[dict] = []

    def apart(c: dict) -> bool:
        return all(c["start_s"] >= k["start_s"] + k["duration_s"] + GAP_S or
                   k["start_s"] >= c["start_s"] + c["duration_s"] + GAP_S for k in chosen)
    for one_a_scene in (True, False):                    # spread over the film first, then fill up
        for c in candidates:
            if len(chosen) >= count:
                break
            if one_a_scene and any(k["scene_id"] == c["scene_id"] for k in chosen):
                continue
            if apart(c):
                chosen.append(c)
    return sorted(chosen, key=lambda c: c["start_s"])


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
    fade = max(0.0, length - 0.25)                        # within FADE_OUT_S: the planner keeps lines clear of it
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

def card_spans(root: Path, edl: dict, chosen=None) -> list[tuple[float, float]]:
    """Where the film's cards lie over its picture (the title over the opening, an INTERVAL), as the cut lays them
    (pipeline._cards): (start, end) in film seconds. `chosen`: the film's cards setting as the run resolved it; None
    reads it as the run would (the defaults with the project's own pipeline.yaml over them). The end card comes after
    the last frame and is no reel's concern."""
    from .cards import Card, auto_cards
    if chosen is None:
        try:
            from ..config import load_pipeline_config
            chosen = load_pipeline_config(Path(root)).get("cards")
        except Exception:
            return []
    try:
        if chosen == "auto":
            first = min((float(d["start_s"]) for d in edl.get("dialogue") or []), default=None)
            cards = auto_cards("", "", first)
        else:
            cards = [Card.from_config(c, Path(root)) for c in chosen or [] if isinstance(c, dict)]
    except Exception:
        return []
    return [(float(c.at_s), float(c.at_s) + float(c.length_s)) for c in cards if not getattr(c, "after", False)]


def film_mark(movie: Path) -> str:
    """The cut a set of reels comes from: a hash of movie.mp4."""
    h = hashlib.sha256()
    with open(movie, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def make_reels(root: Path, count: int = REEL_COUNT, shortest: float = SHORTEST_S, longest: float = LONGEST_S,
               fps: int = 24, log: Callable[[str], None] = print, cards=None) -> str:
    """The film's reels in 11_master/reels/ (reel_1.mp4 ...) with reels.json: where each comes from, its line and a
    caption. Made again only when movie.mp4 or the owner's number changed. Returns what happened, for the run
    report."""
    count = max(1, min(MAX_COUNT, int(count)))
    plan = {"v": PLAN, "count": count, "shortest": shortest, "longest": longest}
    root = Path(root)
    movie, edl_path = root / "11_master" / "movie.mp4", root / "reports" / "edl.json"
    folder = root / "11_master" / FOLDER
    if not movie.exists() or not edl_path.exists():
        return "skipped: no finished film yet"
    mark = film_mark(movie)
    try:
        before = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    if before.get("film") == mark and before.get("plan") == plan and before.get("reels") and \
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
    picked = plan_reels(edl, plans, lines, roles, count=count, shortest=shortest, longest=longest,
                        cards=card_spans(root, edl, cards))
    if not picked:
        return "skipped: no stretch of the film fits a reel"
    import shutil

    from ..service.produce import fingerprint
    work = root / "11_master" / f".{FOLDER}.new"         # cut aside: the reels go in whole, or not at all
    shutil.rmtree(work, ignore_errors=True)
    # a reel of this very cut, at the same place with the same line, is kept as it is (the same file goes up as a
    # Short once): only new ones are encoded
    kept = {(r.get("start_s"), r.get("duration_s"), r.get("line_id")): folder / r["file"]
            for r in before.get("reels") or [] if before.get("film") == mark and (folder / r.get("file", "")).exists()}
    made = []
    try:
        for n, r in enumerate(picked, 1):
            a = round(round(r["start_s"] * fps) / fps, 3)   # on the film's own frame grid, where its cuts are
            length = round(round((r["start_s"] + r["duration_s"]) * fps) / fps - a, 3)
            b = blocks.get(r["line_id"]) if r["line_id"] else None
            te, en = (b.text_te, b.text_en) if b else ("", "")
            out = work / f"reel_{n}.mp4"
            same = kept.get((a, length, r["line_id"]))
            if same is not None:
                work.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(same, out)
            else:
                overlay = render_overlay(work / f"reel_{n}.png", bible.title_te, bible.title_en, te, en)
                out = cut_reel(movie, a, length, overlay, out, fps)
                overlay.unlink(missing_ok=True)
            made.append({"file": out.name, "start_s": a, "duration_s": length, "line_id": r["line_id"],
                         "line_te": te, "line_en": en, "scene_id": r["scene_id"], "caption": caption(bible, te, en),
                         "fp": fingerprint(out)})
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
    (folder / RECORD).write_text(json.dumps({"film": mark, "plan": plan, "reels": made}, indent=1,
                                            ensure_ascii=False), encoding="utf-8")
    return f"{len(made)} made" + (f" (the film has room for {len(made)} whole moments, not {count})"
                                  if len(made) < count else "")
