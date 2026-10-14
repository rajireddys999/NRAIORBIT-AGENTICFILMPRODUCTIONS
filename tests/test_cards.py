"""Movie cards: title, INTERVAL and end overlays (Telugu over English) on the finished movie."""

import copy
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.demo import DEMO_CONFIG, demo_llm
from nraiorbit.pipeline import Pipeline
from nraiorbit.post import cards, ffmpeg
from nraiorbit.post.cards import Card, apply_cards, render_card

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
FPS = 24
TITLE = Card("title", 1.0, 2.0, "గోదావరి సింహం", "GODAVARI SIMHAM")


def _gold(rgb: np.ndarray) -> np.ndarray:
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    return (r > 180) & (g > 140) & (b < 140) & (r - b > 70)


def _movie(path: Path, size: tuple[int, int] = (320, 180), seconds: float = 6.0) -> Path:
    """A textured blue-grey test film with a tone, encoded like the master (H.264 + AAC 48 kHz)."""
    w, h = size
    ffmpeg.run(["-f", "lavfi", "-i", f"color=c=0x5a6e82:s={w}x{h}:r={FPS}:d={seconds},noise=alls=12:allf=t",
                "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                *ffmpeg.VIDEO_ENCODE, "-c:a", "aac", "-ar", "48000", "-shortest", str(path)])
    return path


def _frame(video: Path, t: float) -> np.ndarray:
    out = video.with_name(f"{video.stem}_{t:.2f}.png")
    ffmpeg.run(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-update", "1", str(out)])
    return np.asarray(Image.open(out).convert("RGB"))


def _audio_md5(video: Path) -> str:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _opaque_rows(png: Path) -> np.ndarray:
    alpha = np.asarray(Image.open(png))[..., 3]
    return np.flatnonzero((alpha > 128).any(axis=1))


def test_card_png_is_transparent_with_gold_text(tmp_path):
    png = render_card(TITLE, tmp_path / "title.png", log=lambda m: None)
    img = np.asarray(Image.open(png))
    assert img.shape == (1080, 1920, 4)
    assert img[0, 0, 3] == 0 and img[-1, -1, 3] == 0                  # transparent around the text
    gold = _gold(img[..., :3]) & (img[..., 3] > 200)
    assert gold.sum() > 5000
    small = np.asarray(Image.open(render_card(TITLE, tmp_path / "small.png", (320, 180), log=lambda m: None)))
    assert small.shape == (180, 320, 4) and (_gold(small[..., :3]) & (small[..., 3] > 200)).any()
    interval = render_card(Card("interval", 0, 2, "విరామం", "INTERVAL"), tmp_path / "interval.png", log=lambda m: None)
    if cards.telugu_font():                                            # interval/end set a little smaller than title
        rows, irows = _opaque_rows(png), _opaque_rows(interval)
        assert irows[-1] - irows[0] < rows[-1] - rows[0]


@pytest.mark.skipif(cards.telugu_font() is None, reason="no Telugu font with RAQM shaping here")
def test_telugu_line_is_drawn_above_english(tmp_path):
    both = render_card(TITLE, tmp_path / "both.png", log=lambda m: None)
    english = render_card(Card("title", 1.0, 2.0, "", "GODAVARI SIMHAM"), tmp_path / "en.png", log=lambda m: None)
    assert _opaque_rows(both)[0] < _opaque_rows(english)[0] - 50       # a second, taller line above


def test_telugu_falls_back_to_english_only_without_a_font(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "TELUGU_FONTS", [str(tmp_path / "missing.ttf")])
    logs: list[str] = []
    png = render_card(TITLE, tmp_path / "fallback.png", log=logs.append)
    img = np.asarray(Image.open(png))
    assert any("no Telugu font" in m and "English line only" in m for m in logs)
    assert (_gold(img[..., :3]) & (img[..., 3] > 200)).sum() > 1000    # the English title is there
    rows = _opaque_rows(png)
    assert rows[-1] - rows[0] < 200                                    # one line only
    only_te = render_card(Card("interval", 0, 2, "విరామం", ""), tmp_path / "te.png", log=logs.append)
    assert (np.asarray(Image.open(only_te))[..., 3] == 0).all()        # never boxes in place of Telugu


def test_card_image_replaces_text(tmp_path):
    art = tmp_path / "title_art.png"
    Image.new("RGBA", (960, 540), (200, 30, 30, 255)).save(art)
    png = render_card(Card("title", 0, 2, "గోదావరి సింహం", "GODAVARI SIMHAM", image=art), tmp_path / "c.png", (320, 180))
    img = np.asarray(Image.open(png))
    assert img[90, 160, 0] > 180 and img[90, 160, 1] < 60 and img[90, 160, 3] == 255
    assert not _gold(img[..., :3]).any()


@needs_ffmpeg
def test_cards_overlay_live_picture_and_keep_length_and_sound(tmp_path):
    clean = _movie(tmp_path / "clean.mp4")
    out = tmp_path / "carded.mp4"
    logs: list[str] = []
    end = Card("end", 5.0, 3.0, "గోదావరి సింహం", "GODAVARI SIMHAM")        # runs past the end: clipped
    late = Card("interval", 10.0, 2.0, "విరామం", "INTERVAL")              # after the end: skipped
    apply_cards(clean, [end, TITLE, late], out, (320, 180), FPS, log=logs.append)

    assert abs(ffmpeg.duration(out) - ffmpeg.duration(clean)) <= 1 / FPS
    assert _audio_md5(out) == _audio_md5(clean)                         # the sound is copied untouched
    assert any("interval" in m and "skipped" in m for m in logs)

    for t in (2.0, 5.5):                                                # inside the title and the clipped end card
        inside, before = _frame(out, t).astype(float), _frame(clean, t).astype(float)
        assert inside.mean() < before.mean() * 0.8                      # darkened under the card
        assert _gold(inside.astype(np.uint8)).sum() > 50 and not _gold(before.astype(np.uint8)).any()
    for t in (0.5, 4.0):                                                # outside every card: the picture as it was
        diff = np.abs(_frame(out, t).astype(float) - _frame(clean, t).astype(float))
        assert diff.mean() < 3.0
    assert len(list((tmp_path / ".carded" / "cards").glob("*.png"))) == 2


def _pipeline(tmp_path, card_list):
    root = tmp_path / "godavari"
    root.mkdir(exist_ok=True)
    cfg = deep_merge(load_pipeline_config(), copy.deepcopy(DEMO_CONFIG))
    cfg["cards"] = card_list
    return Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None)


@needs_ffmpeg
def test_pipeline_cards_on_off_never_remaster(tmp_path):
    pipe = _pipeline(tmp_path, [])
    pipe.p.master.mkdir(parents=True)
    movie = _movie(pipe.p.master / "movie.mp4", (640, 360), 4.0)        # the animatic master's size
    original = movie.read_bytes()
    clean, record = pipe.p.edit / "movie_nocards.mp4", pipe.p.edit / "cards.json"

    pipe._place_cards(movie)                                            # no cards: the master exactly as assembled
    assert movie.read_bytes() == original and not clean.exists() and not record.exists()

    title = {"kind": "title", "at_s": 1.0, "length_s": 1.5, "text_te": "గోదావరి సింహం", "text_en": "GODAVARI SIMHAM"}
    pipe = _pipeline(tmp_path, [title])
    pipe._place_cards(movie)
    assert clean.read_bytes() == original and movie.read_bytes() != original
    assert json.loads(record.read_text(encoding="utf-8"))[0]["kind"] == "title"
    assert abs(ffmpeg.duration(movie) - 4.0) <= 1 / FPS
    carded = movie.read_bytes()
    pipe._place_cards(movie)                                            # unchanged cards: nothing to do
    assert movie.read_bytes() == carded

    pipe = _pipeline(tmp_path, [{**title, "at_s": 2.0}])                # moved: re-laid from the clean master
    pipe._place_cards(movie)
    assert movie.read_bytes() not in (carded, original) and clean.read_bytes() == original

    pipe = _pipeline(tmp_path, [])                                      # cards off again: the master is back
    pipe._place_cards(movie)
    assert movie.read_bytes() == original and not clean.exists() and not record.exists()


@needs_ffmpeg
def test_pipeline_never_cards_twice_when_the_clean_master_is_gone(tmp_path, monkeypatch):
    pipe = _pipeline(tmp_path, [])
    pipe.p.master.mkdir(parents=True)
    movie = _movie(pipe.p.master / "movie.mp4", (640, 360), 4.0)
    original = movie.read_bytes()
    clean, record = pipe.p.edit / "movie_nocards.mp4", pipe.p.edit / "cards.json"
    title = {"kind": "title", "at_s": 1.0, "length_s": 1.5, "text_en": "GODAVARI SIMHAM"}
    remastered = []

    def master(self, out):                                              # stands in for assembling the scenes again
        remastered.append(out)
        record.unlink(missing_ok=True)
        out.write_bytes(original)

    monkeypatch.setattr(Pipeline, "_master", master)
    pipe = _pipeline(tmp_path, [title])
    pipe._place_cards(movie)
    once = _frame(movie, 1.75).mean()
    clean.unlink()                                                      # 10_edit's big files cleared to free disk
    pipe._place_cards(movie)
    assert remastered == [movie] and clean.read_bytes() == original
    assert abs(_frame(movie, 1.75).mean() - once) < 3                   # carded once, not twice

    clean.unlink()                                                      # ... and then the cards are taken off
    pipe = _pipeline(tmp_path, [])
    pipe._place_cards(movie)
    assert len(remastered) == 2 and movie.read_bytes() == original
    assert not clean.exists() and not record.exists()


@needs_ffmpeg
def test_pipeline_relays_cards_when_the_image_or_font_changes(tmp_path, monkeypatch):
    pipe = _pipeline(tmp_path, [])
    pipe.p.master.mkdir(parents=True)
    movie = _movie(pipe.p.master / "movie.mp4", (640, 360), 4.0)
    card = {"kind": "title", "at_s": 1.0, "length_s": 1.5, "text_en": "GODAVARI SIMHAM", "image": "inbox/title.png"}
    pipe = _pipeline(tmp_path, [card])
    pipe._place_cards(movie)                                            # the image isn't there yet: text card
    text_card = movie.read_bytes()
    art = pipe.p.root / "inbox" / "title.png"
    art.parent.mkdir(parents=True)
    Image.new("RGBA", (960, 540), (200, 30, 30, 255)).save(art)
    pipe._place_cards(movie)                                            # dropped in later: laid again
    assert movie.read_bytes() != text_card
    assert _frame(movie, 1.75)[180, 320, 0] > 150
    with_art = movie.read_bytes()
    pipe._place_cards(movie)
    assert movie.read_bytes() == with_art                               # unchanged image: nothing to do

    te = Card("title", 1.0, 1.5, "గోదావరి సింహం", "GODAVARI SIMHAM")
    monkeypatch.setattr(cards, "telugu_font", lambda: None)
    without = te.record()
    monkeypatch.setattr(cards, "telugu_font", lambda: "/fonts/NotoSerifTelugu-Bold.ttf")
    assert te.record() != without                                       # a Telugu font installed: laid again
    assert Card("title", 1.0, 1.5, "", "GODAVARI SIMHAM").record()["telugu_font"] is None


def test_card_config_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="card kind"):
        Card.from_config({"kind": "intermission", "at_s": 1, "length_s": 2}, tmp_path)
    card = Card.from_config({"kind": "end", "at_s": 1, "length_s": 2, "image": "inbox/title.png"}, tmp_path)
    assert card.image == tmp_path / "inbox" / "title.png" and card.text_te == ""
