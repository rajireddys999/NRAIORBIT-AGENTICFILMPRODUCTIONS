"""On-screen cards in the finished movie: the title, INTERVAL and the end card, Telugu over English.

A card is an overlay on the live picture for [at_s, at_s + length_s]: the film keeps its length and its sync with
dialogue and music. The picture is darkened under the card and both fade in and out together. Each card is rendered
once as a transparent PNG (Pillow; Telugu needs complex-script shaping, so RAQM) and every card goes onto the movie
in a single ffmpeg pass, the sound copied untouched. A card marked `after` (the end card of `cards: auto`) is added
after the film's last frame instead, on black, with silence: nothing in the film moves.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from . import ffmpeg
from .assemble import TITLE_FONTS

TELUGU_FONTS = [
    "/usr/local/share/fonts/noto-telugu/NotoSerifTelugu-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerifTelugu-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf",
    "/usr/local/share/fonts/noto-telugu/NotoSansTelugu-Bold.ttf",
    "/usr/share/fonts/truetype/lohit-telugu/Lohit-Telugu.ttf",
]
CARD_SIZE = (1920, 1080)                 # design size; scaled to the film's resolution
CARD_GOLD = (232, 190, 90)               # #E8BE5A
CARD_EDGE = (90, 58, 10)                 # thin dark-gold outline, as on the trailer title
CARD_DIM = 0.45                          # the picture under a card is darkened by this much
CARD_FADE_S = 0.4
# kind -> (Telugu px, English px) at 1080 lines
CARD_TEXT_PX = {"title": (150, 62), "interval": (120, 52), "end": (120, 52)}


@dataclass
class Card:
    kind: str                            # title | interval | end
    at_s: float                          # position in the film
    length_s: float
    text_te: str = ""
    text_en: str = ""
    image: Path | None = None            # a designed title image, used instead of the text
    after: bool = False                  # added after the film's last frame (on black), not laid over it

    @classmethod
    def from_config(cls, entry: dict, root: Path) -> Card:
        if entry.get("kind") not in CARD_TEXT_PX:
            raise ValueError(f"card kind must be one of {sorted(CARD_TEXT_PX)}: {entry}")
        image = entry.get("image")
        return cls(entry["kind"], float(entry.get("at_s") or 0), float(entry["length_s"]), entry.get("text_te") or "",
                   entry.get("text_en") or "", root / image if image else None, bool(entry.get("after", False)))

    def record(self) -> dict:
        """What the rendered card depends on: its config, plus the image file and the Telugu font actually used, so
        dropping in or redesigning the image, or installing a Telugu font, lays the cards again."""
        art = self.image.stat() if self.image is not None and self.image.exists() else None
        return {"kind": self.kind, "at_s": self.at_s, "length_s": self.length_s, "text_te": self.text_te,
                "text_en": self.text_en, "image": str(self.image) if self.image else None, "after": self.after,
                "image_file": [art.st_size, art.st_mtime_ns] if art else None,
                "telugu_font": telugu_font() if self.text_te and art is None else None}


TITLE_CARD_S = 4.0                       # cards: auto
END_CARD_S = 4.0


def auto_cards(title_te: str, title_en: str, first_line_s: float | None) -> list[Card]:
    """`cards: auto`: the film's name over its opening, ended before the first line is spoken (1.5 to TITLE_CARD_S
    seconds; none when a line starts sooner, so no speaker is darkened), and an end card after its last frame: శుభం /
    THE END, as Telugu films close."""
    end = Card("end", 0.0, END_CARD_S, "శుభం", "THE END", after=True)
    length = TITLE_CARD_S if first_line_s is None else min(TITLE_CARD_S, first_line_s - 0.8)
    if length < 1.5:
        return [end]
    return [Card("title", 0.3, round(length, 2), title_te or "", (title_en or "").upper()), end]


def telugu_font() -> str | None:
    """The first Telugu font present, if Pillow can shape Telugu (RAQM); else None (never draw broken glyph boxes)."""
    if not features.check("raqm"):
        return None
    return next((f for f in TELUGU_FONTS if Path(f).exists()), None)


def _line(text: str, font: ImageFont.FreeTypeFont, y: int, w: int, text_layer: Image.Image, shadow_layer: Image.Image,
          shadow: int) -> None:
    """Draw one centred line with its top at y: gold on the text layer, black on the (to be blurred) shadow layer."""
    left, top, right, _ = font.getbbox(text)
    x = (w - (right - left)) // 2 - left
    edge = max(1, font.size // 60)
    ImageDraw.Draw(shadow_layer).text((x + shadow, y - top + shadow), text, font=font, fill=(0, 0, 0, 200),
                                      stroke_width=edge * 2, stroke_fill=(0, 0, 0, 200))
    ImageDraw.Draw(text_layer).text((x, y - top), text, font=font, fill=(*CARD_GOLD, 255),
                                    stroke_width=edge, stroke_fill=(*CARD_EDGE, 255))


def render_card(card: Card, out: Path, resolution: tuple[int, int] = CARD_SIZE, log: Callable[[str], None] = print) -> Path:
    """A transparent PNG of the card at `resolution`: Telugu large in gold with a soft shadow, English smaller beneath,
    or the card's designed image fitted to the frame. Without a Telugu font (or RAQM) only the English line is drawn."""
    w, h = CARD_SIZE
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if card.image is not None and card.image.exists():
        art = Image.open(card.image).convert("RGBA")
        scale = min(w / art.width, h / art.height)
        art = art.resize((max(1, round(art.width * scale)), max(1, round(art.height * scale))), Image.Resampling.LANCZOS)
        canvas.alpha_composite(art, ((w - art.width) // 2, (h - art.height) // 2))
    else:
        if card.image is not None:
            log(f"  card {card.kind}: image {card.image} not found, drawing the text instead")
        te_px, en_px = CARD_TEXT_PX[card.kind]
        lines: list[tuple[str, ImageFont.FreeTypeFont]] = []
        te_font = telugu_font() if card.text_te else None
        if card.text_te and te_font:
            lines.append((card.text_te, ImageFont.truetype(te_font, te_px, layout_engine=ImageFont.Layout.RAQM)))
        elif card.text_te:
            log(f"  card {card.kind}: no Telugu font with complex-script shaping (RAQM) found "
                f"(looked for {', '.join(TELUGU_FONTS)}); drawing the English line only")
        en_font = next((f for f in TITLE_FONTS if Path(f).exists()), None) or ffmpeg.font_file()
        if card.text_en and en_font:
            lines.append((card.text_en, ImageFont.truetype(en_font, en_px if lines else te_px * 2 // 3)))
        gap = h // 24
        heights = [f.getbbox(t)[3] - f.getbbox(t)[1] for t, f in lines]
        y = (h - sum(heights) - gap * (len(lines) - 1)) // 2
        shadow_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        text_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for (text, font), height in zip(lines, heights):
            _line(text, font, y, w, text_layer, shadow_layer, shadow=max(2, h // 216))
            y += height + gap
        canvas = Image.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(h // 90)), text_layer)
    if tuple(resolution) != (w, h):
        canvas = canvas.resize(tuple(resolution), Image.Resampling.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def _overlay_frame(card_png: Path, out: Path) -> Path:
    """The card over a black veil at CARD_DIM: one image, so the darkening fades in and out with the card."""
    card = Image.open(card_png).convert("RGBA")
    veil = Image.new("RGBA", card.size, (0, 0, 0, round(255 * CARD_DIM)))
    Image.alpha_composite(veil, card).save(out)
    return out


def apply_cards(source: Path, cards: list[Card], out: Path, resolution: tuple[int, int], fps: int,
                card_dir: Path | None = None, log: Callable[[str], None] = print) -> Path:
    """Lay the cards over `source` in one ffmpeg pass: picture re-encoded, sound copied, length unchanged; then add
    the `after` cards past its last frame (append_cards).

    A card starting after the end of the film is skipped; one running past it is cut at the end.
    """
    after = [c for c in cards if c.after]
    if after:
        laid = apply_cards(source, [c for c in cards if not c.after], out.parent / f".{out.stem}" / f"laid{out.suffix}",
                           resolution, fps, card_dir, log)
        try:
            return append_cards(laid, after, out, resolution, fps, card_dir, log)
        finally:
            laid.unlink(missing_ok=True)                 # a whole copy of the film: never left to be backed up
    work = out.parent / f".{out.stem}"
    card_dir = card_dir or work / "cards"
    for d in (work, card_dir):
        d.mkdir(parents=True, exist_ok=True)
    total = ffmpeg.duration(source)
    inputs, chain, last = ["-i", str(source)], [], "[0:v]"
    for card in sorted(cards, key=lambda c: c.at_s):
        if card.at_s >= total:
            log(f"  card {card.kind} at {card.at_s:.1f} s starts after the end of the film ({total:.1f} s): skipped")
            continue
        a, length = max(0.0, card.at_s), min(card.length_s, total - max(0.0, card.at_s))
        if length <= 0:
            continue
        n = len(chain) // 2 + 1                          # ffmpeg input index of this card
        png = render_card(card, card_dir / f"{n:02d}_{card.kind}.png", resolution, log)
        frame = _overlay_frame(png, work / f"overlay_{n:02d}.png")
        fade = min(CARD_FADE_S, length / 3)
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{length:.3f}", "-i", str(frame)]
        chain.append(f"[{n}:v]format=rgba,fade=t=in:st=0:d={fade:.3f}:alpha=1,"
                     f"fade=t=out:st={length - fade:.3f}:d={fade:.3f}:alpha=1,setpts=PTS-STARTPTS+{a:.3f}/TB[c{n}]")
        chain.append(f"{last}[c{n}]overlay=eof_action=pass:enable='between(t,{a:.3f},{a + length:.3f})'[v{n}]")
        last = f"[v{n}]"
    if not chain:
        shutil.copy2(source, out)
        return out
    script = work / "cards.filter.txt"
    script.write_text(";\n".join(chain) + f";\n{last}format=yuv420p[vout]", encoding="utf-8")
    ffmpeg.run([*inputs, "-filter_complex_script", str(script), "-map", "[vout]", "-map", "0:a?",
                *ffmpeg.VIDEO_ENCODE, "-c:a", "copy", "-t", f"{total:.6f}", str(out)])
    script.unlink()
    return out


def append_cards(source: Path, cards: list[Card], out: Path, resolution: tuple[int, int], fps: int,
                 card_dir: Path | None = None, log: Callable[[str], None] = print) -> Path:
    """Add each card after `source`'s last frame: the card on black for length_s, fading in and out, with silence;
    picture and sound re-encoded once, so the film and the cards play as one file."""
    work = out.parent / f".{out.stem}"
    card_dir = card_dir or work / "cards"
    for d in (work, card_dir):
        d.mkdir(parents=True, exist_ok=True)
    total = ffmpeg.frame_count(source) / fps            # the picture's own length, frame-exact
    w, h = resolution
    inputs = ["-i", str(source)]
    filters = [f"[0:v]fps={fps},scale={w}:{h},format=yuv420p,setsar=1[fv]",       # the film as the cards are made
               f"[0:a]aformat=sample_rates={ffmpeg.SAMPLE_RATE}:channel_layouts=stereo,apad,"
               f"atrim=0:{total:.6f}[fa]"]                                     # its sound exactly as long as its picture
    joined = "[fv][fa]"
    for n, card in enumerate(cards, start=1):
        png = render_card(card, card_dir / f"after_{n:02d}_{card.kind}.png", resolution, log)
        frame = card_dir / f"after_{n:02d}_{card.kind}_on_black.png"
        art = Image.open(png).convert("RGBA")
        Image.alpha_composite(Image.new("RGBA", art.size, (0, 0, 0, 255)), art).convert("RGB").save(frame)
        length, fade = card.length_s, min(CARD_FADE_S, card.length_s / 3)
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{length:.3f}", "-i", str(frame),
                   "-f", "lavfi", "-t", f"{length:.3f}", "-i", f"anullsrc=r={ffmpeg.SAMPLE_RATE}:cl=stereo"]
        filters.append(f"[{2 * n - 1}:v]scale={w}:{h},fade=t=in:st=0:d={fade:.3f},"
                       f"fade=t=out:st={length - fade:.3f}:d={fade:.3f},format=yuv420p,setsar=1[cv{n}]")
        filters.append(f"[{2 * n}:a]aformat=sample_rates={ffmpeg.SAMPLE_RATE}:channel_layouts=stereo[ca{n}]")
        joined += f"[cv{n}][ca{n}]"
    script = work / "append.filter.txt"
    script.write_text(";\n".join([*filters, f"{joined}concat=n={len(cards) + 1}:v=1:a=1[vout][aout]"]),
                      encoding="utf-8")
    ffmpeg.run([*inputs, "-filter_complex_script", str(script), "-map", "[vout]", "-map", "[aout]",
                *ffmpeg.VIDEO_ENCODE, "-r", str(fps), "-c:a", "aac", "-b:a", "256k", "-ar", str(ffmpeg.SAMPLE_RATE),
                str(out)])
    script.unlink()
    log(f"  cards after the film: {', '.join(f'{c.kind} ({c.length_s:.1f} s)' for c in cards)}")
    return out
