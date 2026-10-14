"""Lyria 3 music provider against a fake Google client: prompt, fit to the cue, cache, budget, inbox, preview."""

import json
import subprocess
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from nraiorbit.config import deep_merge, load_navarasa, load_pipeline_config
from nraiorbit.costing import BudgetExceeded, SpendLedger, load_pricing, preview_run
from nraiorbit.poc import create_poc
from nraiorbit.post import ffmpeg
from nraiorbit.providers import google_ai
from nraiorbit.providers.lyria import LyriaMusic, cache_dir, cache_key, cue_prompt, cue_seconds
from nraiorbit.schemas import Cue

pytest.importorskip("google.genai")
needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
ARJUN = {"arjun": ("Arjun", "hero")}


def cue(**kw) -> Cue:
    base = dict(cue_id="cue002", scene_ids=["sc002"], start_s=25.0, duration_s=10.0, rasa="veera", intensity=8,
                cue_type="hero_elevation", raga="nattai", tala="adi", bpm=141, instruments=["dappu", "thavil", "nadaswaram"],
                leitmotif_character="arjun", peak=False, silence_before_s=1.5)
    return Cue(**{**base, **kw})


def audio_bytes(tmp, seconds: float, mime: str) -> bytes:
    if mime.startswith("audio/L16"):                      # raw little-endian PCM, as Gemini sends speech
        t = np.arange(int(24000 * seconds)) / 24000
        return (np.sin(2 * np.pi * 330 * t) * 8000).astype("<i2").tobytes()
    path = tmp / f"gen_{seconds}{'.mp3' if 'mpeg' in mime else '.wav'}"
    ffmpeg.run(["-f", "lavfi", "-i", f"sine=f=220:d={seconds}", "-ar", "44100", "-ac", "1", str(path)])
    return path.read_bytes()


class FakeLyria:
    """generate_content as google-genai returns it: a text part, then the audio as inline_data."""

    def __init__(self, tmp=None, seconds=12.0, mime="audio/wav", text_only=False):
        self.mime, self.text_only, self.calls = mime, text_only, []
        self.audio = b"" if text_only else audio_bytes(tmp, seconds, mime)
        self.models = SimpleNamespace(generate_content=self._content)

    def _content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        parts = [SimpleNamespace(text="Heroic Telugu score in Nattai.", inline_data=None)]
        if not self.text_only:
            parts.append(SimpleNamespace(text=None, inline_data=SimpleNamespace(data=self.audio, mime_type=self.mime)))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason="STOP")],
                               prompt_feedback=None)


@pytest.fixture
def root(tmp_path):
    return create_poc(tmp_path / "godavari5")


def lyria_cfg(root, **google):
    return deep_merge(load_pipeline_config(root), {"providers": {"music": "lyria"}, "google": google})


def provider(root, fake, budget=None, **google):
    ledger = SpendLedger(root / "reports" / "cost.jsonl", budget)
    return LyriaMusic(root, lyria_cfg(root, **google), load_pricing(root), ledger, load_navarasa(), client=fake), ledger


def lyria_records(root):
    return [r for r in SpendLedger(root / "reports" / "cost.jsonl", None).records() if r["provider"] == "lyria"]


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["sample_rate"]), int(s["channels"]), ffmpeg.duration(path)


def test_prompt_carries_raga_instruments_and_notes():
    nav = load_navarasa()
    bed = cue(rasa="bhakti", cue_type="temple", raga="mayamalavagowla", instruments=["nadaswaram", "thavil", "temple_bells"])
    p = cue_prompt(bed, chars=ARJUN, navarasa=nav)
    assert p.startswith("Instrumental Telugu film background score (BGM). Instrumental only, no vocals")
    assert "Mayamalavagowla" in p and "devotional, dramatic minor colour" in p and "C Db E F G Ab B" in p
    assert "piercing nadaswaram, thunderous thavil, temple bells" in p and "about 141 BPM" in p and "Adi tala" in p
    assert "leitmotif for Arjun, the hero" in p and "Length: about 8 seconds." in p
    assert "Leave space for dialogue" in p and "Structure" not in p and "Mass elevation" not in p

    trailer = cue(cue_id="trailer", cue_type="trailer", peak=True, silence_before_s=0.0, duration_s=44.2,
                  leitmotif_character=None, notes="0-8 s low drone; 8-20 s rising dappu roll; 21 s HIT")
    p = cue_prompt(trailer, navarasa=nav)
    assert "Structure (follow these timings): 0-8 s low drone; 8-20 s rising dappu roll; 21 s HIT." in p
    assert "thunderous thavil and dappu" in p and "brass stabs" in p and "wordless" in p
    assert "Leave space for dialogue" not in p and "Arc:" not in p        # the notes replace the generic arc
    assert "Nattai" in p and "C D# E F G A# B" in p and "about 44 seconds" in p
    assert "Arc:" in cue_prompt(trailer.model_copy(update={"notes": ""}))

    long_bed = cue(duration_s=61.5, peak=False)
    assert "about 30 seconds, seamlessly loopable" in cue_prompt(long_bed, model="lyria-3-clip-preview")
    assert "about 60 seconds." in cue_prompt(long_bed, model="lyria-3-pro-preview")


@needs_ffmpeg
def test_longer_track_is_trimmed_with_a_fade(root, tmp_path):
    fake = FakeLyria(tmp_path, seconds=12.0)
    music, _ = provider(root, fake)
    c = cue()
    out = music.render_cue(c, root / "08_music" / "cue002.wav")
    rate, channels, seconds = probe(out)
    assert (rate, channels) == (48000, 2) and seconds == pytest.approx(cue_seconds(c), abs=0.02)   # 10 s - 1.5 s silence
    call = fake.calls[0]
    assert call["model"] == "lyria-3-pro-preview" and call["contents"] == music.prompt(c)
    assert call["config"].response_modalities == ["AUDIO", "TEXT"]          # Lyria 3 refuses AUDIO alone
    with wave.open(str(out)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
    assert np.abs(pcm[-960:]).mean() < 0.1 * np.abs(pcm[: len(pcm) // 2]).mean()                  # faded out
    assert [p.suffix for p in cache_dir(root).iterdir()] == [".wav"]


@needs_ffmpeg
@pytest.mark.parametrize("mime, seconds, ext", [("audio/mpeg", 5.0, ".mp3"), ("audio/L16;codec=pcm;rate=24000", 4.0, ".wav")])
def test_shorter_track_is_looped_with_crossfades(root, tmp_path, mime, seconds, ext):
    fake = FakeLyria(tmp_path, seconds=seconds, mime=mime)
    music, _ = provider(root, fake)
    c = cue(duration_s=21.5)
    rate, channels, out_s = probe(music.render_cue(c, root / "08_music" / "cue002.wav"))
    assert (rate, channels) == (48000, 2) and out_s == pytest.approx(20.0, abs=0.02)
    raw = next(cache_dir(root).iterdir())
    assert raw.suffix == ext and ffmpeg.duration(raw) == pytest.approx(seconds, abs=0.1)


@needs_ffmpeg
def test_cached_generation_is_never_paid_twice(root, tmp_path):
    fake = FakeLyria(tmp_path)
    music, _ = provider(root, fake)
    music.render_cue(cue(), root / "08_music" / "cue002.wav")
    again, _ = provider(root, fake)                        # a later run: new provider, same cue
    again.render_cue(cue(), root / "08_music" / "cue002_again.wav")
    assert len(fake.calls) == 1
    assert lyria_records(root) == [{"provider": "lyria", "item": "cue002", "usd": 0.5, "model": "lyria-3-pro-preview",
                                    "mime": "audio/wav"}]
    again.render_cue(cue(notes="hit at 4 s"), root / "08_music" / "cue002.wav")   # new direction = new prompt
    assert len(fake.calls) == 2


def test_budget_stops_before_the_call(root):
    fake = FakeLyria(text_only=True)
    music, _ = provider(root, fake, budget=0.30)          # Lyria 3 Pro is priced 0.50 per track
    with pytest.raises(BudgetExceeded, match="Lyria cue002"):
        music.render_cue(cue(), root / "08_music" / "cue002.wav")
    assert fake.calls == [] and lyria_records(root) == [] and not cache_dir(root).exists()


@needs_ffmpeg
def test_inbox_track_wins_without_a_call(root, tmp_path):
    inbox = root / "inbox" / "music"
    inbox.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-f", "lavfi", "-i", "sine=f=110:d=20", str(inbox / "cue002.mp3")])
    fake = FakeLyria(text_only=True)
    music, _ = provider(root, fake)
    out = music.render_cue(cue(), root / "08_music" / "cue002.wav")
    assert ffmpeg.duration(out) == pytest.approx(8.5, abs=0.05)
    assert fake.calls == [] and lyria_records(root) == []


def test_text_only_response_stops_the_run(root):
    fake = FakeLyria(text_only=True)
    music, _ = provider(root, fake)
    with pytest.raises(google_ai.GoogleAPIError, match="Lyria cue002: no audio in the response.*STOP.*Heroic"):
        music.render_cue(cue(), root / "08_music" / "cue002.wav")
    assert len(fake.calls) == 1 and not list(cache_dir(root).glob("*"))
    assert [r["usd"] for r in lyria_records(root)] == [0.0]
    off, _ = provider(root, fake, music_response_modalities=None)
    with pytest.raises(google_ai.GoogleAPIError):
        off.render_cue(cue(), root / "08_music" / "cue002.wav")
    assert fake.calls[-1]["config"] is None                # modalities can be switched off for the paid test


def test_lyria_call_matches_the_real_sdk():
    """The fake mirrors these fields; fail loudly if google-genai changes them."""
    import inspect

    from google.genai import types
    from google.genai.models import Models

    assert {"model", "contents", "config"} <= set(inspect.signature(Models.generate_content).parameters)
    assert types.GenerateContentConfig(response_modalities=["AUDIO"]).response_modalities == ["AUDIO"]
    assert {"data", "mime_type"} <= set(types.Blob.model_fields) and {"inline_data", "text"} <= set(types.Part.model_fields)
    assert {"content", "finish_reason"} <= set(types.Candidate.model_fields)


def test_music_client_uses_music_location_and_retries(monkeypatch, root):
    import sys

    from google.genai import types

    made = []

    class FakeGenaiClient:
        def __init__(self, **kw):
            made.append(kw)

    fake_genai = SimpleNamespace(Client=FakeGenaiClient, types=types)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setattr(sys.modules["google"], "genai", fake_genai, raising=False)
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", None)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "film-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    google_ai.google_client({"vertex": True, "music_location": "europe-west4"}, "music")
    assert made[-1]["location"] == "europe-west4" and made[-1]["http_options"].retry_options.attempts == 5
    music, _ = provider(root, None, vertex=True, music_location=None)
    assert len(made) == 1                                   # no client until a paid call needs one
    assert music.client is not None and len(made) == 2
    assert made[-1]["location"] == "global" and made[-1]["vertexai"] is True   # Lyria 3 is served from global only
    assert made[-1]["http_options"].retry_options.attempts == 5


def test_preview_counts_lyria_cues(root, tmp_path, capsys):
    from nraiorbit import cli

    pricing = load_pricing(root)
    mock = preview_run(root, load_pipeline_config(root), pricing)
    assert (mock["cues"], mock["music_usd"]) == (0, 0.0)
    cfg = lyria_cfg(root)
    pre = preview_run(root, cfg, pricing)
    assert (pre["cues"], pre["cached_cues"]) == (10, 0)   # 9 cues in the sheet + the trailer's cue
    assert pre["music_usd"] == pytest.approx(5.0) and pre["total_usd"] == pytest.approx(mock["total_usd"] + 5.0)
    assert preview_run(root, lyria_cfg(root, music_model="lyria-3-clip-preview"), pricing)["music_usd"] == pytest.approx(1.0)

    (root / "inbox" / "music").mkdir(parents=True, exist_ok=True)
    (root / "inbox" / "music" / "cue001.wav").write_bytes(b"")
    pre = preview_run(root, cfg, pricing)
    assert (pre["cues"], pre["cached_cues"]) == (9, 1)

    cli._preflight(root, cfg, assume_yes=True)
    assert "Music (Lyria): 9 new cues -> $4.50  (1 already generated or in inbox/music, free)" in capsys.readouterr().out


@needs_ffmpeg
def test_cached_cue_counts_as_free_in_the_preview(root, tmp_path):
    sheet = json.loads((root / "08_music" / "cue_sheet.json").read_text())
    c = Cue(**sheet["cues"][1])
    music, _ = provider(root, FakeLyria(tmp_path))
    music.render_cue(c, root / "08_music" / f"{c.cue_id}.wav")
    pre = preview_run(root, lyria_cfg(root), load_pricing(root))
    assert (pre["cues"], pre["cached_cues"]) == (9, 1)


@needs_ffmpeg
def test_undecodable_generation_stops_with_a_hint(root):
    music, _ = provider(root, FakeLyria(text_only=True))
    c = cue()
    cache_dir(root).mkdir(parents=True)
    bad = cache_dir(root) / f"{cache_key(music.model, music.prompt(c))}.wav"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(google_ai.GoogleAPIError, match="cannot decode.*Delete it"):
        music.render_cue(c, root / "08_music" / "cue002.wav")
    assert music._client.calls == [] and lyria_records(root) == []


@needs_ffmpeg
def test_pipeline_scores_film_and_trailer_with_lyria(tmp_path, monkeypatch):
    from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
    from nraiorbit.pipeline import Pipeline

    root = tmp_path / "godavari"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = deep_merge(load_pipeline_config(), deep_merge(DEMO_CONFIG, {"providers": {"music": "lyria"},
                                                                      "film": {"resolution": [160, 90]}}))
    fake = FakeLyria(tmp_path, seconds=30.0)
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", lambda cfg: fake)
    Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None).run()
    sheet = [Cue.model_validate(c) for c in json.loads((root / "08_music" / "cue_sheet.json").read_text())["cues"]]
    scored = [c for c in sheet if c.instruments]
    assert len(fake.calls) == len(scored) + 1                             # every scored cue, then the trailer's
    for c in scored:
        assert ffmpeg.duration(root / "08_music" / f"{c.cue_id}.wav") == pytest.approx(cue_seconds(c), abs=0.02)
    trailer = fake.calls[-1]["contents"]
    assert "Cue: trailer." in trailer and "Mass elevation" in trailer and "Nattai" in trailer
    master = root / "11_master"
    assert ffmpeg.duration(root / "08_music" / "trailer_cue.wav") == pytest.approx(
        ffmpeg.duration(master / "trailer.mp4"), abs=0.1)
    records = lyria_records(root)
    assert records[-1]["item"] == "trailer" and sum(r["usd"] for r in records) == pytest.approx(0.5 * len(fake.calls))

    calls = len(fake.calls)                                               # re-cut: same plan, same prompt, free
    Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None).run(force=("trailer",))
    assert len(fake.calls) == calls and len(lyria_records(root)) == calls and (master / "trailer.mp4").exists()


def test_budget_stop_on_a_music_cue_ends_the_run_cleanly(root, tmp_path, monkeypatch):
    from nraiorbit import cli
    from nraiorbit.pipeline import Pipeline

    fake = FakeLyria(text_only=True)
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", lambda cfg: fake)
    cfg = deep_merge(load_pipeline_config(root), {"providers": {"video": "mock", "tts": "mock", "music": "lyria"},
                                                  "film": {"resolution": [160, 90]}, "budget": {"max_usd": 0.30}})
    pipeline = Pipeline(root, llm=None, cfg=cfg, log=lambda m: None)
    monkeypatch.setattr(pipeline, "_render_line", lambda *a: (tmp_path / "line.wav", 1.0))   # music is what matters
    monkeypatch.setattr(pipeline, "_render_shot", lambda *a: tmp_path / "shot.mp4")
    with pytest.raises(SystemExit, match="Blocked: budget .*Lyria cue001"):
        cli._run(pipeline, root.name, until="render")
    assert fake.calls == []


@needs_ffmpeg
def test_generated_hit_is_aligned_to_the_picture(root, tmp_path):
    """Generators drift: a hit planned at 0:10 that arrives at 14 s is moved back onto the hero reveal."""
    from nraiorbit.providers.lyria import find_hit, fit_to_cue, planned_hit

    src = tmp_path / "late_hit.wav"
    rate = 48000
    t = np.arange(int(rate * 30)) / rate
    quiet = 0.01 * np.sin(2 * np.pi * 110 * t)
    loud = np.where(t >= 14.0, 0.6 * np.sin(2 * np.pi * 80 * t), 0.0)
    pcm = ((quiet + loud) * 32767).astype("<i2")
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    c = cue().model_copy(update={"duration_s": 20.0, "silence_before_s": 0.0,
                                 "notes": "[0:00 - 0:09] Build: x. [0:09 - 0:10] Drop: y. [0:10 - 0:12] Hit: z."})
    assert planned_hit(c) == 10
    assert find_hit(src, 10) == pytest.approx(14.0, abs=0.1)
    out = fit_to_cue(src, 20.0, tmp_path / "aligned.wav", start=find_hit(src, 10) - 10)
    with wave.open(str(out)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)[::2]
    hop = 4800
    level = [np.abs(a[i:i + hop]).mean() for i in range(0, len(a) - hop, hop)]
    assert np.argmax(np.diff(level)) + 1 in (99, 100, 101)        # the hit now starts at 10.0 s (100 x 0.1 s)
    assert find_hit(tmp_path / "aligned.wav", 10) == pytest.approx(10.0, abs=0.1)
    assert planned_hit(cue()) is None
