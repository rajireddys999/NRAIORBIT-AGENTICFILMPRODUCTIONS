"""Title and end cards: the owner's words -> a deck -> cards drawn and joined around the untouched film, the step
that holds for the words and for a look, and the release delivery and YouTube take."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.demo import CARD_DECK, DEMO_CONFIG, DEMO_PLOT, STORY_BIBLE, demo_llm
from nraiorbit.pipeline import Pipeline
from nraiorbit.post import ffmpeg, titlecards as tc
from nraiorbit.post.reels import film_mark
from nraiorbit.schemas import CardDeck, CardLine, CardSlide, StoryBible
from nraiorbit.service import produce

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
FPS = 24
GOLD = tc.PALETTES["gold"]


def _gold(rgb: np.ndarray) -> np.ndarray:
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    return (r > 170) & (g > 130) & (b < 150) & (r - b > 60)


def _movie(path: Path, size=(320, 180), seconds=3.0) -> Path:
    """A textured test film with a tone, encoded like the master (H.264 + AAC 48 kHz stereo)."""
    w, h = size
    ffmpeg.run(["-f", "lavfi", "-i", f"color=c=0x5a6e82:s={w}x{h}:r={FPS}:d={seconds},noise=alls=12:allf=t",
                "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                "-af", "aformat=channel_layouts=stereo", *ffmpeg.VIDEO_ENCODE, "-r", str(FPS),
                "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-shortest", str(path)])
    return path


def _frame(video: Path, t: float) -> np.ndarray:
    out = video.with_name(f"{video.stem}_{t:.2f}.png")
    ffmpeg.run(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-update", "1", str(out)])
    return np.asarray(Image.open(out).convert("RGB"))


def _pipeline(tmp_path, gates="ask", list_cards=True):
    root = tmp_path / "demo"
    (root / "input").mkdir(parents=True, exist_ok=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    review = {"cards": {"enabled": True, "auto_approve": False}} if list_cards else {}
    cfg = deep_merge(copy.deepcopy(DEMO_CONFIG), {"review_gates": review, "policy": {"gates": gates, "publish": "none"}})
    (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    p = Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda *_: None)
    p.p.root.mkdir(exist_ok=True)
    (p.p.root / "01_story_bible.json").write_text(StoryBible.model_validate(STORY_BIBLE).model_dump_json(), encoding="utf-8")
    return p


def _ask(root: Path, brief="the title, then THE END", none=False, rid="r1") -> dict:
    req = {"id": rid, "brief": brief, "none": none, "by": "owner@example.com", "at": "2026-11-01T10:00:00+00:00"}
    (root / tc.REQUEST).write_text(json.dumps(req), encoding="utf-8")
    return req


# ------------------------------------------------------------------ the owner's words

def test_the_suggested_words_name_the_film_its_cast_and_the_end():
    text = tc.suggested_brief(STORY_BIBLE, "demo")
    assert "గోదావరి సింహం / Godavari Simham" in text and STORY_BIBLE["logline"] in text
    assert "Arjun (అర్జున్) as the hero" in text and "THE END" in text and text.startswith("Before the film:")
    assert tc.suggested_brief(None, "rain-ferry").count("rain-ferry") == 1        # no bible yet: the slug stands in


def test_a_request_is_read_only_when_it_says_something(tmp_path):
    assert tc.read_request(tmp_path) is None
    (tmp_path / tc.REQUEST).write_text("not json")
    assert tc.read_request(tmp_path) is None
    (tmp_path / tc.REQUEST).write_text(json.dumps({"id": "x", "brief": "   "}))
    assert tc.read_request(tmp_path) is None                                      # nothing asked for
    (tmp_path / tc.REQUEST).write_text(json.dumps({"brief": "words"}))
    assert tc.read_request(tmp_path) is None                                      # no id: never applied
    (tmp_path / tc.REQUEST).write_text(json.dumps({"id": "x", "brief": " the title " + "x" * 3000}))
    req = tc.read_request(tmp_path)
    assert req["brief"].startswith("the title") and len(req["brief"]) == tc.BRIEF_MAX and req["none"] is False
    (tmp_path / tc.REQUEST).write_text(json.dumps({"id": "y", "none": True}))
    assert tc.read_request(tmp_path)["none"] is True


def test_the_words_gate_is_asked_only_where_the_project_lists_it():
    assert not tc.asks_for_words({})
    assert not tc.asks_for_words({"review_gates": {"story": {}}})
    assert tc.asks_for_words({"review_gates": {"cards": {}}})
    assert tc.asks_for_words({"review_gates": {"cards": {"auto_approve": False}}})
    assert not tc.asks_for_words({"review_gates": {"cards": {"auto_approve": True}}})
    assert not tc.asks_for_words({"review_gates": {"cards": {"enabled": False}}})
    assert tc.asks_for_words(yaml.safe_load((Path(__file__).parents[1] / "projects" / "_template" / "pipeline.yaml")
                                            .read_text(encoding="utf-8")))     # a queued film stops for the words


# ------------------------------------------------------------------ the deck

def test_tidy_clamps_lengths_drops_empty_lines_and_caps_the_counts():
    line = {"text_te": "", "text_en": "x", "size": "medium"}
    deck = CardDeck.model_validate({"style": "gold", "backdrop": "black", "why": "", "slides": [
        {"where": "after", "kind": "end", "seconds": 99, "lines": [line]},
        {"where": "before", "kind": "studio", "seconds": 0.5, "lines": [line, {"text_te": " ", "text_en": "", "size": "small"}]},
        {"where": "before", "kind": "about", "seconds": 4, "lines": [{"text_te": "", "text_en": "", "size": "small"}]},
        *[{"where": "before", "kind": "credit", "seconds": 3, "lines": [line] * 9} for _ in range(5)],
        *[{"where": "after", "kind": "cast", "seconds": 3, "lines": [line]} for _ in range(9)],
    ]})
    tidy = tc.tidy(deck)
    wheres = [s.where for s in tidy.slides]
    assert wheres == ["before"] * tc.MOST_BEFORE + ["after"] * tc.MOST_AFTER     # before first, then after; capped
    assert tidy.slides[0].seconds == tc.MIN_S and len(tidy.slides[0].lines) == 1   # the blank line went
    assert tidy.slides[tc.MOST_BEFORE].seconds == tc.MAX_S                         # the end card, clamped
    assert all(len(s.lines) <= tc.MOST_LINES for s in tidy.slides)
    assert tc.tidy(CardDeck.model_validate({**CARD_DECK, "slides": []})).slides == []


def test_a_card_is_drawn_in_gold_on_a_dark_ground_with_a_rule_under_the_title(tmp_path):
    title = CardSlide(where="before", kind="title", seconds=4,
                      lines=[CardLine(text_te="గోదావరి సింహం", text_en="Godavari Simham", size="title")])
    png = tc.render_card(title, tmp_path / "title.png", GOLD, log=lambda m: None)
    img = np.asarray(Image.open(png).convert("RGB"))
    assert img.shape == (1080, 1920, 3)
    assert img[10, 10].max() < 40 and img[-10, -10].max() < 40                 # dark corners
    assert _gold(img).sum() > 4000                                              # the lettering
    rows = np.flatnonzero(_gold(img).sum(axis=1) > 300)                        # the rule: a full-width gold band
    assert rows.size and (rows.max() - rows.min()) > 60                        # letters and, lower, the rule
    small = np.asarray(Image.open(tc.render_card(title, tmp_path / "s.png", GOLD, (320, 180), log=lambda m: None)))
    assert small.shape == (180, 320, 3)
    royal = np.asarray(Image.open(tc.render_card(title, tmp_path / "r.png", tc.PALETTES["royal"], (320, 180),
                                                 log=lambda m: None)).convert("RGB"))
    assert royal[5, 5][2] > royal[5, 5][0] + 20                                 # a blue ground
    cast = CardSlide(where="after", kind="cast", seconds=5, lines=[CardLine(text_te="", text_en=f"Name {i}", size="medium")
                                                                   for i in range(12)])
    many = np.asarray(Image.open(tc.render_card(cast, tmp_path / "cast.png", GOLD, log=lambda m: None)).convert("RGB"))
    assert _gold(many).sum() > 1000 and not _gold(many[:20]).any() and not _gold(many[-20:]).any()   # fitted inside


def test_the_title_sits_over_the_films_still_darkened(tmp_path):
    still = tmp_path / "still.png"
    Image.new("RGB", (640, 360), (200, 180, 160)).save(still)
    title = CardSlide(where="before", kind="title", seconds=4, lines=[CardLine(text_te="", text_en="Rain", size="title")])
    img = np.asarray(Image.open(tc.render_card(title, tmp_path / "t.png", GOLD, (640, 360), still, log=lambda m: None)))
    assert 30 < img[5, 5].mean() < 80                                           # the still shows, at a quarter


def test_subtitles_move_later_by_the_opening_cards(tmp_path):
    src = tmp_path / "subs.srt"
    src.write_text("1\n00:00:01,500 --> 00:00:03,250\nనమస్తే\n\n2\n00:59:58,900 --> 01:00:02,000\nhello\n", encoding="utf-8")
    out = tc.shift_srt(src, tmp_path / "moved" / "subs.srt", 11.0)
    text = out.read_text(encoding="utf-8")
    assert "00:00:12,500 --> 00:00:14,250" in text and "01:00:09,900 --> 01:00:13,000" in text
    assert "నమస్తే" in text and "hello" in text


# ------------------------------------------------------------------ the step: words, a look, kept, remade

def test_the_step_waits_for_words_where_listed_then_for_a_look_then_keeps_the_cards(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, list_cards=True)
    p.p.master.mkdir(parents=True)
    (p.p.master / "movie.mp4").write_bytes(b"the cut")
    made = []

    def fake_make(pipeline, req, mark, log=print):
        made.append(req["id"])
        folder = pipeline.p.master / tc.FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        rec = {"request_id": req["id"], "film": mark, "opening_s": 7.0, "closing_s": 8.0,
               "slides": [{"file": "a.png", "where": "before", "seconds": 7.0},
                          {"file": "b.png", "where": "after", "seconds": 8.0}]}
        (folder / tc.RECORD).write_text(json.dumps(rec))
        (pipeline.p.master / tc.RELEASE).write_bytes(b"cut with cards")
        return rec
    monkeypatch.setattr(tc, "make", fake_make)
    pol = produce.policy_of(p.cfg)
    assert tc.step(p, pol) == ("awaiting", "awaiting:cards")                      # no words yet: asked for
    assert (p.p.reviews / "cards.pending").read_text().startswith("Say what the cards")
    produce.approve(p, "cards")                                                     # the owner: go on without cards
    assert tc.step(p, pol)[0] == "none" and not (p.p.reviews / "cards.pending").exists()
    _ask(p.p.root, rid="r1")                                                        # words arrive (the row approved)
    assert tc.step(p, pol) == ("awaiting", "awaiting:cards") and made == ["r1"]    # made, and held for a look
    assert not (p.p.reviews / "cards.approved").exists()                            # the old approval is spent
    assert (p.p.reviews / "cards.pending").read_text().startswith("Watch the film with its cards")
    assert tc.step(p, pol) == ("awaiting", "awaiting:cards") and made == ["r1"]    # still waiting: not made twice
    produce.approve(p, "cards")
    assert tc.step(p, pol) == ("kept", "2 cards: 7 s before the film, 8 s after") and made == ["r1"]
    _ask(p.p.root, brief="other words", rid="r2")                                   # new words: made again, a new look
    assert tc.step(p, pol)[0] == "awaiting" and made == ["r1", "r2"]
    produce.approve(p, "cards")
    (p.p.master / "movie.mp4").write_bytes(b"a new cut")                            # a re-cut: made again for it
    assert tc.step(p, pol)[0] == "awaiting" and made == ["r1", "r2", "r2"]
    produce.approve(p, "cards")
    assert tc.step(p, pol)[0] == "kept"
    _ask(p.p.root, none=True, rid="r3")                                             # the owner takes the cards off
    assert tc.step(p, pol)[0] == "none" and not (p.p.master / tc.RELEASE).exists()
    assert not (p.p.master / tc.FOLDER).exists() and tc.release(p.p.root) is None


def test_a_project_that_never_lists_the_gate_goes_on_without_cards_until_words_come(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, list_cards=False)
    p.p.master.mkdir(parents=True)
    (p.p.master / "movie.mp4").write_bytes(b"the cut")
    pol = produce.policy_of(p.cfg)
    assert tc.step(p, pol)[0] == "none" and not (p.p.reviews / "cards.pending").exists()
    monkeypatch.setattr(tc, "make", lambda pipeline, req, mark, log=print: (
        (pipeline.p.master / tc.FOLDER).mkdir(parents=True, exist_ok=True),
        (pipeline.p.master / tc.FOLDER / tc.RECORD).write_text(json.dumps(
            {"request_id": req["id"], "film": mark, "opening_s": 3, "closing_s": 3, "slides": [{}]})),
        (pipeline.p.master / tc.RELEASE).write_bytes(b"x"))[0] or tc.record_of(pipeline.p.root))
    _ask(p.p.root)
    assert tc.step(p, pol) == ("awaiting", "awaiting:cards")                       # words given: a look is asked
    auto = produce.policy_of(p.cfg, gates="auto")
    assert tc.step(p, auto)[0] == "kept"                                           # under auto nothing waits


def test_the_release_speaks_only_for_the_cut_it_was_made_for(tmp_path):
    root = tmp_path / "film"
    (root / "11_master" / tc.FOLDER).mkdir(parents=True)
    movie = root / "11_master" / "movie.mp4"
    movie.write_bytes(b"cut one")
    assert tc.release(root) is None
    (root / "11_master" / tc.RELEASE).write_bytes(b"cut one with cards")
    rec = {"request_id": "r1", "film": film_mark(movie), "opening_s": 6.5, "closing_s": 4, "slides": [{}, {}]}
    (root / "11_master" / tc.FOLDER / tc.RECORD).write_text(json.dumps(rec))
    (root / "11_master" / tc.FOLDER / "subs_te.srt").write_text("1\n")
    out = tc.release(root)
    assert out["film"] == root / "11_master" / tc.RELEASE and out["opening_s"] == 6.5 and out["count"] == 2
    assert list(out["captions"]) == ["te"]
    movie.write_bytes(b"cut two")                                                   # a new cut: stale cards never ship
    assert tc.release(root) is None


# ------------------------------------------------------------------ delivery and YouTube take the film with cards

def test_delivery_and_the_upload_take_the_film_with_its_cards_and_move_the_chapters(tmp_path, monkeypatch):
    from nraiorbit.demo import OUTLINE
    from nraiorbit.publish import youtube
    from nraiorbit.release import deliver
    from nraiorbit.schemas import Outline
    root = tmp_path / "film"
    (root / "11_master" / tc.FOLDER).mkdir(parents=True)
    (root / "01_story_bible.json").write_text(StoryBible.model_validate(STORY_BIBLE).model_dump_json(), encoding="utf-8")
    (root / "02_outline.json").write_text(Outline.model_validate(OUTLINE).model_dump_json(), encoding="utf-8")
    movie, release = root / "11_master" / "movie.mp4", root / "11_master" / tc.RELEASE
    movie.write_bytes(b"the film")
    release.write_bytes(b"the film with cards")
    (root / "11_master" / "subs_en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    (root / "11_master" / tc.FOLDER / "subs_en.srt").write_text("1\n00:00:13,000 --> 00:00:14,000\nhi\n")
    (root / "11_master" / tc.FOLDER / tc.RECORD).write_text(json.dumps(
        {"request_id": "r1", "film": film_mark(movie), "opening_s": 12.0, "closing_s": 5.0, "slides": [{}]}))
    monkeypatch.setattr(deliver, "encode_720p", lambda src, out: out.write_bytes(b"small:" + src.read_bytes()) or out)
    recs = deliver.deliver(root, upload=False, date="2026-11-01", log=lambda *_: None)
    assert Path(recs[0]["local"]) == release                                        # the 1080p file is the one with cards
    assert (root / "11_master" / "deliver" / recs[1]["name"]).read_bytes() == b"small:the film with cards"
    assert json.loads((root / "11_master" / "deliver" / "delivery.json").read_text())["cut"]["movie"] == film_mark(release)

    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS", str(tmp_path / "missing.json"))
    monkeypatch.setattr(youtube, "token_available", lambda token: True)
    monkeypatch.setattr(youtube, "credentials", lambda secrets, token, interactive=True: "creds")
    sent = []
    monkeypatch.setattr(youtube, "upload", lambda video, meta, creds, captions=None, thumbnail=None:
                        sent.append((video.name, meta["snippet"]["description"], dict(captions or {}))) or f"id-{len(sent)}")
    out = produce.publish_youtube(root, "private")
    assert sent[0][0] == tc.RELEASE and out["film"]["cards"] is True and out["film"]["file"] == produce.fingerprint(release)
    assert sent[0][2] == {"en": root / "11_master" / tc.FOLDER / "subs_en.srt"}   # the moved subtitles
    plain = youtube.build_metadata(StoryBible.model_validate(STORY_BIBLE), Outline.model_validate(OUTLINE))
    moved = youtube.build_metadata(StoryBible.model_validate(STORY_BIBLE), Outline.model_validate(OUTLINE), offset_s=12.0)
    assert plain["snippet"]["description"] != moved["snippet"]["description"] and sent[0][1] == moved["snippet"]["description"]
    assert produce.published_already(root)                                          # this very film with cards is up
    movie.write_bytes(b"a new cut")                                                 # the cards go stale with it
    assert tc.release(root) is None and not produce.published_already(root)


def test_chapters_move_later_by_the_opening_and_the_first_stays_at_zero():
    from nraiorbit.demo import OUTLINE
    from nraiorbit.publish import youtube
    from nraiorbit.schemas import Outline
    outline = Outline.model_validate(OUTLINE)
    plain, moved = youtube.chapters(outline), youtube.chapters(outline, 9.0)
    if plain:
        assert moved[0] == (0.0, "Opening") and [t for t, _ in moved[1:]] == [t + 9.0 for t, _ in plain[1:]]
    else:
        assert moved == []


# ------------------------------------------------------------------ the whole thing, with ffmpeg

@needs_ffmpeg
def test_the_cards_are_joined_around_the_untouched_film(tmp_path, monkeypatch):
    """The demo deck (3 cards before, 2 after) around a 3-second film: the film's frames inside are the film's own,
    the cards show gold on dark, the length adds up, the subtitles move, and the film file itself is left alone."""
    p = _pipeline(tmp_path, list_cards=True)
    p.p.master.mkdir(parents=True)
    movie = _movie(p.p.master / "movie.mp4")
    before = movie.read_bytes()
    (p.p.master / "subs_en.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nhello\n", encoding="utf-8")
    passes, real = [], ffmpeg.run
    monkeypatch.setattr(ffmpeg, "run", lambda args: (passes.append(args), real(args))[1])
    req = _ask(p.p.root, brief="the studio line, the title over a still, one line about the story; the cast and THE END")
    rec = tc.make(p, req, film_mark(movie), log=lambda *_: None)
    assert movie.read_bytes() == before                                             # the film is not touched
    assert not any(str(movie) in a and a[-1].endswith(tc.RELEASE) and "-filter_complex_script" in a
                   for a in passes)                                                  # the film is not encoded again
    opening = sum(s["seconds"] for s in CARD_DECK["slides"] if s["where"] == "before")
    closing = sum(s["seconds"] for s in CARD_DECK["slides"] if s["where"] == "after")
    assert rec["opening_s"] == opening == 11.0 and rec["closing_s"] == closing == 8.0 and len(rec["slides"]) == 5
    out = p.p.master / tc.RELEASE
    assert abs(ffmpeg.duration(out) - (3.0 + opening + closing)) <= 0.5
    assert ffmpeg.frame_count(out) == ffmpeg.frame_count(movie) + round((opening + closing) * FPS)
    first = _frame(out, 1.5)                                                        # the studio card: gold on dark
    assert _gold(first).sum() > 20 and np.median(first) < 25
    inside = _frame(out, opening + 1.5).astype(float)                               # the film, where it was
    assert np.abs(inside - _frame(movie, 1.5).astype(float)).mean() < 3.0
    end = _frame(out, opening + 3.0 + closing - 1.5)                                # THE END
    assert _gold(end).sum() > 20 and np.median(end) < 25
    assert "00:00:11,500 --> 00:00:12,500" in (p.p.master / tc.FOLDER / "subs_en.srt").read_text(encoding="utf-8")
    pngs = sorted(f.name for f in (p.p.master / tc.FOLDER).glob("*.png"))
    assert pngs == ["01_before_studio.png", "02_before_title.png", "03_before_about.png", "04_after_cast.png",
                    "05_after_end.png"]
    assert tc.release(p.p.root)["count"] == 5 and not (p.p.master / f".{tc.FOLDER}").exists()


@needs_ffmpeg
def test_a_produced_film_stops_for_the_words_makes_the_cards_and_ships_them(tmp_path, monkeypatch):
    """The whole run under the template's gates: after QA the run waits for the owner's words; with them it makes the
    cards and waits for a look; approved, it delivers the film with its cards and asks for the upload."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    root = tmp_path / "demo"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = deep_merge(copy.deepcopy(DEMO_CONFIG), {"review_gates": {"cards": {"enabled": True, "auto_approve": False}},
                                                  "policy": {"gates": "ask", "publish": "private"}})
    (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    p = Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda *_: None)
    uploads = []
    monkeypatch.setattr(produce, "publish_youtube", lambda root, privacy, bucket=None: uploads.append(privacy) or {"film": "x"})
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "awaiting:cards" and report["steps"]["cards"] == "awaiting:cards"
    assert "deliver" not in report["steps"] and not uploads
    _ask(root, brief="the title, the cast, THE END")                                 # the control room's words
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "awaiting:cards" and (p.p.master / tc.RELEASE).exists()
    produce.approve(p, "cards")
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "awaiting:publish" and report["steps"]["cards"].startswith("5 cards:")
    delivered = json.loads((p.p.master / "deliver" / "delivery.json").read_text(encoding="utf-8"))
    assert delivered["cut"]["movie"] == film_mark(p.p.master / tc.RELEASE)          # the downloads carry the cards
    assert p.llm.calls.count(("card_writer", "CardDeck")) == 1 if hasattr(p, "llm") else True
