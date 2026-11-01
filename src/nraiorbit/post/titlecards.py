"""Title and end cards: the cards shown before and after the finished film, written from the owner's own words.

The owner says in plain words what the cards should carry (the film's name, the cast, a few words about the story,
THE END, a look); the card writer (agents/cards.py, one Claude call) turns that into a deck (schemas.CardDeck); each
card is drawn once with Pillow, Telugu over English in gold as the trailer's title is, and encoded as a short clip
with silence exactly as the master is encoded, so the clips and the film join by stream copy: the film itself is
never touched or encoded again, however long it is, and nothing but the Claude call is paid for.

The join is 11_master/movie_with_cards.mp4, beside the film as cut (movie.mp4, which the reels, the QA and the
subtitles keep reading). Delivery and the YouTube upload take the film with its cards when it is there and was made
for the current cut (release()); the subtitles and the chapters move with it by the length of the opening cards.

The owner's words live beside the story (stories/<slug>.title_cards.json, written by the control room; the tick
copies them into the project as title_cards.json every run), so a new cut of the film gets its cards again from the
same words, and the step then asks the owner for a look again. The step (step()) is the runner's, between QA and
delivery: see service/produce.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from ..schemas import CardDeck, CardSlide, StoryBible
from . import ffmpeg
from .assemble import TITLE_FONTS
from .cards import TELUGU_FONTS
from .reels import film_mark

GATE = "cards"                               # the review gate and the control room's stage key
REQUEST = "title_cards.json"                 # in the project: the owner's words, as the tick last copied them
SUFFIX = ".title_cards.json"                 # stories/<slug>.title_cards.json: where the control room writes them
FOLDER = "title_cards"                       # under 11_master/: the drawn cards, the record and the moved subtitles
RECORD = "title_cards.json"                  # in FOLDER: what was made, from which words and for which cut
RELEASE = "movie_with_cards.mp4"             # under 11_master/: the film with its cards before and after
BRIEF_MAX = 2000
MIN_S, MAX_S = 2.0, 10.0
MOST_BEFORE, MOST_AFTER, MOST_LINES = 4, 8, 6
FADE_S = 0.6
SIZE = (1920, 1080)                          # design size; drawn at the film's resolution
# style -> lettering, its outline, the background
PALETTES = {
    "gold": {"text": (232, 190, 90), "edge": (90, 58, 10), "back": (0, 0, 0)},
    "ivory": {"text": (243, 235, 211), "edge": (72, 62, 44), "back": (0, 0, 0)},
    "crimson": {"text": (232, 190, 90), "edge": (90, 58, 10), "back": (46, 6, 10)},
    "royal": {"text": (232, 190, 90), "edge": (90, 58, 10), "back": (8, 18, 48)},
}
# line size -> (Telugu px, English px) at 1080 lines
SIZES = {"title": (150, 64), "large": (96, 50), "medium": (64, 40), "small": (44, 32)}
ENGLISH_FONTS = [*TITLE_FONTS,
                 "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",           # a laptop
                 "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"]
NO_WORDS_YET = ("Say what the cards before and after the film should show (its name, the cast, a few words about "
                "the story, THE END), then press Make the cards in the control room; or go on without them.\n")
LOOK = "Watch the film with its cards, then approve; or change the words and make them again.\n"


# ------------------------------------------------------------------ the owner's words

def read_request(root: Path) -> dict | None:
    """The owner's request as the tick copied it: {"id", "brief", "none", "by", "at"}; `none` when they chose no
    cards. None when there is no request or it cannot be read."""
    try:
        req = json.loads((Path(root) / REQUEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(req, dict) or not req.get("id"):
        return None
    brief = str(req.get("brief") or "").strip()[:BRIEF_MAX]
    none = bool(req.get("none"))
    if not brief and not none:
        return None
    return {"id": str(req["id"]), "brief": brief, "none": none, "by": req.get("by"), "at": req.get("at")}


def suggested_brief(bible: dict | None, slug: str = "") -> str:
    """What the control room offers to start from: the film's name, its one line, its cast and THE END."""
    b = bible if isinstance(bible, dict) else {}
    title_te, title_en = str(b.get("title_te") or "").strip(), str(b.get("title_en") or slug).strip()
    title = " / ".join(t for t in (title_te, title_en) if t)
    cast = []
    for c in b.get("characters") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name_en") or c.get("id") or "").strip()
        te = str(c.get("name_te") or "").strip()
        role = str(c.get("role") or "").replace("_", " ")
        cast.append(f"{name}{f' ({te})' if te else ''}{f' as the {role}' if role else ''}")
    lines = ["Before the film:", "- 'NRAIORBIT Agentic Film Productions presents'", f"- the title: {title}"]
    if b.get("logline"):
        lines.append(f"- one line about the story: {str(b['logline']).strip()}")
    lines += ["", "After the film:"]
    if cast:
        lines.append(f"- the cast: {'; '.join(cast)}")
    lines += ["- 'Story, screenplay, direction and music by AI agents'", "- THE END (శుభం)", "",
              "Style: gold lettering on black, cinematic and calm; the title over a still from the film."]
    return "\n".join(lines)


# ------------------------------------------------------------------ drawing

def telugu_font() -> str | None:
    """The first Telugu font present, if Pillow can shape Telugu (RAQM); else None, and only the English is drawn."""
    if not features.check("raqm"):
        return None
    return next((f for f in TELUGU_FONTS if Path(f).exists()), None)


def english_font() -> str | None:
    return next((f for f in ENGLISH_FONTS if Path(f).exists()), None) or ffmpeg.font_file()


def tidy(deck: CardDeck) -> CardDeck:
    """The deck within what the renderer draws: lengths clamped, empty lines dropped, at most MOST_BEFORE cards
    before and MOST_AFTER after, MOST_LINES lines each, and the cards before the film first."""
    before, after = [], []
    for s in deck.slides:
        lines = [ln for ln in s.lines if ln.text_te.strip() or ln.text_en.strip()][:MOST_LINES]
        if not lines:
            continue
        slide = CardSlide(where=s.where, kind=s.kind, lines=lines,
                          seconds=round(min(MAX_S, max(MIN_S, float(s.seconds or 0))), 2))
        (before if s.where == "before" else after).append(slide)
    return deck.model_copy(update={"slides": before[:MOST_BEFORE] + after[:MOST_AFTER]})


def _background(palette: dict, backdrop: Path | None, size: tuple[int, int]) -> Image.Image:
    """The card's ground: the palette colour with a soft warm glow at the centre, or the film's still blurred and
    darkened to a quarter under the title (never text on a bright picture)."""
    w, h = size
    if backdrop is not None and backdrop.exists():
        art = Image.open(backdrop).convert("RGB")
        scale = max(w / art.width, h / art.height)
        art = art.resize((max(1, round(art.width * scale)), max(1, round(art.height * scale))), Image.Resampling.LANCZOS)
        art = art.crop(((art.width - w) // 2, (art.height - h) // 2, (art.width - w) // 2 + w, (art.height - h) // 2 + h))
        art = art.filter(ImageFilter.GaussianBlur(max(6, h // 60)))
        return Image.fromarray((np.asarray(art, dtype=np.float32) * 0.28).astype(np.uint8))
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    glow = np.clip(1 - d, 0, 1)[..., None] ** 2
    back = np.array(palette["back"], dtype=np.float32)
    rgb = back + np.array([28, 22, 14], dtype=np.float32) * glow
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")


def _draw_line(text: str, font: ImageFont.FreeTypeFont, y: int, w: int, text_layer: Image.Image,
               shadow_layer: Image.Image, shadow: int, fill: tuple, edge: tuple) -> None:
    """One centred line with its top at y: the lettering on the text layer, black on the (to be blurred) shadow."""
    left, top, right, _ = font.getbbox(text)
    x = (w - (right - left)) // 2 - left
    outline = max(1, font.size // 60)
    ImageDraw.Draw(shadow_layer).text((x + shadow, y - top + shadow), text, font=font, fill=(0, 0, 0, 200),
                                      stroke_width=outline * 2, stroke_fill=(0, 0, 0, 200))
    ImageDraw.Draw(text_layer).text((x, y - top), text, font=font, fill=(*fill, 255),
                                    stroke_width=outline, stroke_fill=(*edge, 255))


def _fitted(text: str, font: ImageFont.FreeTypeFont, width: int) -> ImageFont.FreeTypeFont:
    """The font, smaller when the line would run wider than `width`."""
    left, _, right, _ = font.getbbox(text)
    if right - left <= width or font.size <= 16:
        return font
    size = max(16, int(font.size * width / (right - left)))
    return ImageFont.truetype(font.path, size, layout_engine=font.layout_engine)


def render_card(slide: CardSlide, out: Path, palette: dict, resolution: tuple[int, int] = SIZE,
                backdrop: Path | None = None, log: Callable[[str], None] = print) -> Path:
    """One card as a PNG at `resolution`: its lines centred, Telugu above its English, a thin rule under the film's
    name, lettering with a soft shadow on the card's ground. Without a Telugu font (or RAQM) only the English is
    drawn, and a Telugu-only line falls back to its English or is left out, said in the log."""
    w, h = SIZE
    te_font, en_font = telugu_font(), english_font()
    rows: list[tuple[str, ImageFont.FreeTypeFont, bool]] = []       # (text, font, a rule follows)
    for line in slide.lines:
        te_px, en_px = SIZES[line.size]
        te, en = line.text_te.strip(), line.text_en.strip()
        if te and te_font:
            rows.append((te, ImageFont.truetype(te_font, te_px, layout_engine=ImageFont.Layout.RAQM), False))
        elif te and not en:
            log(f"  card: no Telugu font with complex-script shaping (RAQM); the line {te!r} is left out")
        if en and en_font:
            drawn_te = bool(te and te_font)                  # English alone takes most of the Telugu line's size
            rows.append((en.upper() if line.size in ("title", "large") else en,
                         ImageFont.truetype(en_font, en_px if drawn_te else int(te_px * 0.8)), False))
        if rows and line.size == "title":
            rows[-1] = (rows[-1][0], rows[-1][1], True)
    rows = [(t, _fitted(t, f, int(0.86 * w)), r) for t, f, r in rows]     # no line runs off the card
    gap, rule_h = h // 28, h // 12
    heights = [f.getbbox(t)[3] - f.getbbox(t)[1] for t, f, _ in rows]
    total = sum(heights) + gap * max(0, len(rows) - 1) + sum(rule_h for _, _, r in rows if r)
    if total > 0.82 * h:                                          # too many words: smaller, all alike
        k = 0.82 * h / total
        rows = [(t, ImageFont.truetype(f.path, max(16, int(f.size * k)), layout_engine=f.layout_engine), r)
                for t, f, r in rows]
        heights = [f.getbbox(t)[3] - f.getbbox(t)[1] for t, f, _ in rows]
        total = sum(heights) + gap * max(0, len(rows) - 1) + sum(rule_h for _, _, r in rows if r)
    canvas = _background(palette, backdrop, (w, h)).convert("RGBA")
    shadow_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    text_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    y = (h - total) // 2
    for (text, font, rule), height in zip(rows, heights):
        _draw_line(text, font, y, w, text_layer, shadow_layer, max(2, h // 216), palette["text"], palette["edge"])
        y += height
        if rule:                                                  # a thin rule under the film's name
            ImageDraw.Draw(text_layer).rectangle([w * 0.41, y + rule_h // 2 - 1, w * 0.59, y + rule_h // 2 + 1],
                                                 fill=(*palette["text"], 230))
            y += rule_h
        y += gap
    canvas.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(h // 90)))
    canvas.alpha_composite(text_layer)
    card = canvas.convert("RGB")
    if tuple(resolution) != (w, h):
        card = card.resize(tuple(resolution), Image.Resampling.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    card.save(out)
    return out


def film_still(movie: Path, out: Path) -> Path | None:
    """One frame from a third of the way into the film, for the title card's ground; None when it cannot be read."""
    try:
        at = max(0.0, ffmpeg.duration(movie) / 3)
        ffmpeg.run(["-ss", f"{at:.3f}", "-i", str(movie), "-frames:v", "1", "-update", "1", str(out)])
    except Exception:
        return None
    return out if out.exists() else None


# ------------------------------------------------------------------ clips and the join

def card_clip(png: Path, seconds: float, out: Path, resolution: tuple[int, int], fps: int) -> Path:
    """The card as a clip encoded like the master (H.264 as VIDEO_ENCODE, AAC 256k stereo 48 kHz, the film's fps and
    size), faded in and out, silent: it joins the film by stream copy."""
    w, h = resolution
    fade = min(FADE_S, seconds / 3)
    ffmpeg.run(["-loop", "1", "-framerate", str(fps), "-t", f"{seconds:.3f}", "-i", str(png),
                "-f", "lavfi", "-t", f"{seconds:.3f}", "-i", f"anullsrc=r={ffmpeg.SAMPLE_RATE}:cl=stereo",
                "-vf", f"scale={w}:{h},fade=t=in:st=0:d={fade:.3f},fade=t=out:st={seconds - fade:.3f}:d={fade:.3f},"
                       "format=yuv420p,setsar=1",
                "-frames:v", str(max(1, int(round(seconds * fps)))), *ffmpeg.VIDEO_ENCODE, "-r", str(fps),
                "-c:a", "aac", "-b:a", "256k", "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", "-shortest", str(out)])
    return out


def join(before: list[Path], movie: Path, after: list[Path], out: Path, resolution: tuple[int, int], fps: int,
         log: Callable[[str], None] = print) -> Path:
    """The cards, the film and the cards as one file. By stream copy first: the film is not encoded again (a long
    film encoded whole once ran the runner out of memory). When the copy does not add up to the parts' length, the
    whole is encoded once instead, as the card pass over the film does."""
    parts = [*before, movie, *after]
    want = sum(ffmpeg.duration(p) for p in parts)
    try:
        ffmpeg.concat(parts, out)
        got = ffmpeg.duration(out)
    except Exception as exc:                              # a copy the muxer refused
        log(f"  cards: the stream copy failed ({type(exc).__name__}: {str(exc)[:80]}); encoding the join once")
        got = None
    if got is not None and abs(got - want) <= 0.5:
        return out
    if got is not None:
        log(f"  cards: the stream copy runs {got:.2f} s against {want:.2f} s of parts; encoding the join once")
    out.unlink(missing_ok=True)
    w, h = resolution
    inputs, chain, joined = [], [], ""
    for i, p in enumerate(parts):
        inputs += ["-i", str(p)]
        chain.append(f"[{i}:v]fps={fps},scale={w}:{h},format=yuv420p,setsar=1[v{i}]")
        chain.append(f"[{i}:a]aformat=sample_rates={ffmpeg.SAMPLE_RATE}:channel_layouts=stereo[a{i}]")
        joined += f"[v{i}][a{i}]"
    script = out.parent / f".{out.stem}.filter.txt"
    script.write_text(";\n".join([*chain, f"{joined}concat=n={len(parts)}:v=1:a=1[vout][aout]"]), encoding="utf-8")
    ffmpeg.run([*inputs, "-filter_complex_script", str(script), "-map", "[vout]", "-map", "[aout]",
                *ffmpeg.VIDEO_ENCODE, "-r", str(fps), "-c:a", "aac", "-b:a", "256k", "-ar", str(ffmpeg.SAMPLE_RATE),
                str(out)])
    script.unlink()
    return out


_TIMES = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})")


def shift_srt(src: Path, out: Path, offset_s: float) -> Path:
    """The subtitle file moved later by the opening cards' length (every cue; the text as it is)."""
    def later(m: re.Match) -> str:
        def one(h, mi, s, ms):
            total = (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000 + int(ms) + int(round(offset_s * 1000))
            h2, rem = divmod(total, 3_600_000)
            m2, rem = divmod(rem, 60_000)
            s2, ms2 = divmod(rem, 1000)
            return f"{h2:02d}:{m2:02d}:{s2:02d},{ms2:03d}"
        g = m.groups()
        return f"{one(*g[:4])} --> {one(*g[4:])}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_TIMES.sub(later, src.read_text(encoding="utf-8")), encoding="utf-8")
    return out


# ------------------------------------------------------------------ what was made

def record_of(root: Path) -> dict | None:
    try:
        rec = json.loads((Path(root) / "11_master" / FOLDER / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("request_id") else None


def release(root: Path) -> dict | None:
    """The film to deliver and upload when it has cards: {"film", "opening_s", "closing_s", "captions", "count",
    "record"}; None when the cards were made for another cut (or never), and the film goes out as cut. The record
    names the cut it was made for (a hash of movie.mp4), so a restored or re-cut film never ships stale cards."""
    master = Path(root) / "11_master"
    movie, film, rec = master / "movie.mp4", master / RELEASE, record_of(root)
    if rec is None or not movie.exists() or not film.exists() or rec.get("film") != film_mark(movie):
        return None
    captions = {lang: master / FOLDER / f"subs_{lang}.srt" for lang in ("te", "en")
                if (master / FOLDER / f"subs_{lang}.srt").exists()}
    return {"film": film, "opening_s": float(rec.get("opening_s") or 0), "closing_s": float(rec.get("closing_s") or 0),
            "captions": captions, "count": len(rec.get("slides") or []), "record": rec}


def words(rec: dict) -> str:
    """The record in the owner's words: '5 cards: 12 s before the film, 18 s after'."""
    n = len(rec.get("slides") or [])
    return (f"{n} card{'s' if n != 1 else ''}: {float(rec.get('opening_s') or 0):.0f} s before the film, "
            f"{float(rec.get('closing_s') or 0):.0f} s after")


def remove(root: Path) -> None:
    """No cards: the joined film and the folder go (the owner chose none, or the request was withdrawn)."""
    master = Path(root) / "11_master"
    (master / RELEASE).unlink(missing_ok=True)
    shutil.rmtree(master / FOLDER, ignore_errors=True)


def make(pipeline, req: dict, mark: str, log: Callable[[str], None] = print) -> dict:
    """Write the deck from the owner's words (one Claude call), draw every card, encode each as a clip and join them
    around the film; move the subtitles; write the record. The folder is cleared first: a deck goes in whole."""
    master = pipeline.p.master
    movie, folder, work = master / "movie.mp4", master / FOLDER, master / f".{FOLDER}"
    bible = StoryBible.model_validate_json(pipeline.p.bible.read_text(encoding="utf-8"))
    deck = tidy(pipeline.card_writer.write(bible, req["brief"]))
    if not deck.slides:
        raise ValueError("the card writer returned no cards for these words")
    shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    folder.mkdir(parents=True)
    work.mkdir(parents=True)
    resolution = tuple(pipeline.cfg.get("animatic", {}).get("resolution", [640, 360])) if pipeline._animatic() \
        else tuple(pipeline._film["resolution"])
    fps = int(pipeline._film["fps"])
    still = film_still(movie, work / "still.png") if deck.backdrop == "film" else None
    palette = PALETTES.get(deck.style) or PALETTES["gold"]
    before, after, made = [], [], []
    for i, slide in enumerate(deck.slides, 1):
        png = render_card(slide, folder / f"{i:02d}_{slide.where}_{slide.kind}.png", palette, resolution,
                          still if slide.kind == "title" else None, log)
        clip = card_clip(png, slide.seconds, work / f"{i:02d}.mp4", resolution, fps)
        (before if slide.where == "before" else after).append(clip)
        made.append({"file": png.name, "where": slide.where, "kind": slide.kind, "seconds": slide.seconds,
                     "lines": [{"text_te": ln.text_te, "text_en": ln.text_en, "size": ln.size} for ln in slide.lines]})
    join(before, movie, after, master / RELEASE, resolution, fps, log)
    opening = round(sum(s["seconds"] for s in made if s["where"] == "before"), 2)
    closing = round(sum(s["seconds"] for s in made if s["where"] == "after"), 2)
    for lang in ("te", "en"):
        if (master / f"subs_{lang}.srt").exists():
            shift_srt(master / f"subs_{lang}.srt", folder / f"subs_{lang}.srt", opening)
    shutil.rmtree(work, ignore_errors=True)
    rec = {"request_id": req["id"], "brief": req["brief"], "by": req.get("by"), "film": mark,
           "film_bytes": movie.stat().st_size,         # the control room tells a new cut by size, without a download
           "opening_s": opening,
           "closing_s": closing, "style": deck.style, "backdrop": deck.backdrop, "slides": made, "why": deck.why,
           "made": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "release": RELEASE}
    (folder / RECORD).write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"  cards: {words(rec)} ({deck.style}, {deck.backdrop}) -> {master / RELEASE}")
    return rec


def asks_for_words(cfg: dict) -> bool:
    """Whether a run stops for the owner's words when none are given: the project's review_gates list `cards` (as
    Pipeline.gate reads a listed gate: enabled, not auto-approved). A project that never mentions it goes on without
    cards until the owner writes some in the control room."""
    gates = cfg.get("review_gates") or {}
    g = gates.get(GATE) if isinstance(gates, dict) else None
    if g is None:
        return False
    g = g if isinstance(g, dict) else {}
    return bool(g.get("enabled", True)) and not bool(g.get("auto_approve", False))


def step(pipeline, policy: dict, log: Callable[[str], None] = print) -> tuple[str, str]:
    """The runner's cards step, between QA and delivery: (what happened, words for the report). Under `ask`, a
    project whose review_gates list `cards` (the control room's template does) holds here until the owner has written
    the cards or chosen none; any film holds once cards are made, for a look. Under `auto` the cards are made from
    the words there are and the run goes on. Cards made for an earlier cut, or from earlier words, are made again."""
    from ..service.produce import approved, clear, hold
    root = pipeline.p.root
    ask = policy.get("gates") == "ask"
    req, rec = read_request(root), record_of(root)
    if req is None:
        if ask and asks_for_words(pipeline.cfg) and not approved(pipeline, GATE):
            hold(pipeline, GATE, NO_WORDS_YET)
            return "awaiting", f"awaiting:{GATE}"
        clear(pipeline, GATE)
        return "none", "none: no cards asked for; the film goes out as cut"
    if req["none"]:
        if rec is not None or (pipeline.p.master / RELEASE).exists():
            remove(root)
            log("  cards: none, as the owner chose; the film goes out as cut")
        clear(pipeline, GATE)
        return "none", "none, as the owner chose; the film goes out as cut"
    mark = film_mark(pipeline.p.master / "movie.mp4")
    current = rec is not None and rec.get("request_id") == req["id"] and rec.get("film") == mark \
        and (pipeline.p.master / RELEASE).exists()
    if current:
        if ask and not approved(pipeline, GATE):
            hold(pipeline, GATE, LOOK)
            return "awaiting", f"awaiting:{GATE}"
        clear(pipeline, GATE)
        return "kept", words(rec)
    rec = make(pipeline, req, mark, log)
    if not ask:
        clear(pipeline, GATE)
        return "made", words(rec)
    clear(pipeline, GATE, "approved")                     # new cards: a fresh look before anything ships
    hold(pipeline, GATE, LOOK)
    return "awaiting", f"awaiting:{GATE}"
