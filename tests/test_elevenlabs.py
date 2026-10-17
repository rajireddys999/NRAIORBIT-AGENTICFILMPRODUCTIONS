"""ElevenLabs voices: cast map, delivery tags, caching, ledger, the language-code fallback, the budget stop."""

from types import SimpleNamespace

import pytest

from nraiorbit.costing import BudgetExceeded, SpendLedger
from nraiorbit.post import ffmpeg
from nraiorbit.providers import elevenlabs
from nraiorbit.providers.elevenlabs import ElevenLabsError, ElevenLabsTTS, tagged_text
from nraiorbit.schemas import ScriptBlock

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")


class FakeEleven:
    """Answers every text-to-speech call with a short tone; refuses language_code when told to."""

    def __init__(self, tmp_path, refuse_language=False):
        wav = tmp_path / "tone.wav"
        ffmpeg.run(["-f", "lavfi", "-t", "1.2", "-i", "sine=f=220:r=44100", str(wav)])
        self.audio = wav.read_bytes()
        self.refuse_language = refuse_language
        self.calls = []

    def post(self, url, json=None, params=None, timeout=None):
        self.calls.append({"url": url, "body": json, "params": params})
        if self.refuse_language and "language_code" in json:
            return SimpleNamespace(status_code=422, text="language_code not supported", content=b"")
        return SimpleNamespace(status_code=200, text="", content=self.audio)


def _line(character="rudra", rasa="raudra", volume="shout", notes="", text="ఈ ఊరు నాది."):
    return ScriptBlock.model_validate({
        "block_id": "sc001_b003", "kind": "dialogue", "character_id": character, "text_te": text,
        "text_en": "This village is mine.", "delivery": {"rasa": rasa, "intensity": 8, "pace": "medium",
                                                         "volume": volume, "notes": notes}})


def _tts(tmp_path, fake, budget=None, cast=None, voices=None, **extra):
    cfg = {"voice": {"cast": cast if cast is not None else {"rudra": "voice-rudra"}, "max_pause_s": None, **extra}}
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", budget)
    return ElevenLabsTTS(voices or {}, cfg, {"elevenlabs": {"per_1k_chars": 0.18}}, ledger, http=fake), ledger


def test_tags_come_from_the_delivery():
    assert tagged_text(_line()) == "[furious] [shouting] ఈ ఊరు నాది."
    assert tagged_text(_line(rasa="shanta", volume="normal")) == "[calm] ఈ ఊరు నాది."
    assert tagged_text(_line(notes="she screams it")) == "[screams] ఈ ఊరు నాది."


def test_synthesize_uses_the_cast_voice_bills_characters_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(elevenlabs, "SLEEP", lambda s: None)
    fake = FakeEleven(tmp_path)
    tts, ledger = _tts(tmp_path, fake, budget=5.0)
    out = tmp_path / "05_dialogue" / "sc001_b003" / "take_1.wav"
    seconds = tts.synthesize(_line(), "voices/rudra_voice@v1", out)
    assert out.exists() and 1.1 < seconds < 1.3
    call = fake.calls[0]
    assert call["url"].endswith("/text-to-speech/voice-rudra") and call["params"] == {"output_format": "mp3_44100_128"}
    assert call["body"]["model_id"] == "eleven_v4" and call["body"]["language_code"] == "te"
    assert call["body"]["text"].startswith("[furious] [shouting] ")
    rows = ledger.records()
    assert len(rows) == 1 and rows[0]["provider"] == "elevenlabs" and rows[0]["characters"] == len(call["body"]["text"])
    assert rows[0]["usd"] == pytest.approx(len(call["body"]["text"]) * 0.18 / 1000, abs=1e-4)
    # the same line again: served from the cache, no call, no charge
    tts.synthesize(_line(), "voices/rudra_voice@v1", tmp_path / "again.wav")
    assert len(fake.calls) == 1 and len(ledger.records()) == 1


def test_voice_resolution_order_and_clear_error(tmp_path):
    fake = FakeEleven(tmp_path)
    manifest = {"voices/meera_voice@v1": {"elevenlabs_voice": "voice-from-manifest"}}
    tts, _ = _tts(tmp_path, fake, cast={}, voices=manifest)
    assert tts.voice_id(_line(character="meera"), "voices/meera_voice@v1") == "voice-from-manifest"
    tts, _ = _tts(tmp_path, fake, cast={"meera": "voice-from-cast"}, voices=manifest)
    assert tts.voice_id(_line(character="meera"), "voices/meera_voice@v1") == "voice-from-cast"
    tts, _ = _tts(tmp_path, fake, cast={})
    with pytest.raises(ElevenLabsError, match="voice.cast.kaasi"):
        tts.voice_id(_line(character="kaasi"), None)


def test_language_code_is_dropped_when_the_model_refuses_it(tmp_path, monkeypatch):
    monkeypatch.setattr(elevenlabs, "SLEEP", lambda s: None)
    fake = FakeEleven(tmp_path, refuse_language=True)
    tts, _ = _tts(tmp_path, fake)
    assert tts.synthesize(_line(), None, tmp_path / "out.wav").__class__ is float
    assert len(fake.calls) == 2 and "language_code" not in fake.calls[1]["body"]


def test_budget_stops_before_the_call_and_missing_key_is_clear(tmp_path, monkeypatch):
    fake = FakeEleven(tmp_path)
    tts, _ = _tts(tmp_path, fake, budget=0.0001)
    with pytest.raises(BudgetExceeded):
        tts.synthesize(_line(), None, tmp_path / "out.wav")
    assert fake.calls == []
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    bare = ElevenLabsTTS({}, {"voice": {"cast": {"rudra": "v"}}}, {}, SpendLedger(tmp_path / "c.jsonl", None))
    with pytest.raises(ElevenLabsError, match="ELEVENLABS_API_KEY"):
        bare.http()


def test_chants_go_to_the_crowd_provider(tmp_path):
    class Crowd:
        def synthesize_crowd(self, line, voice_ref, out, voices):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"")
            return 2.5
    tts, _ = _tts(tmp_path, FakeEleven(tmp_path))
    assert not tts.can_chant(_line())
    tts.crowd = Crowd()
    assert tts.can_chant(_line()) and tts.synthesize_crowd(_line(), None, tmp_path / "chant.wav", 6) == 2.5
