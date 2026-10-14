"""Crowd lines (voice.crowd in pipeline.yaml): a chant of several Gemini voices, against a fake Google client."""

import wave
from types import SimpleNamespace

import numpy as np
import pytest

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.costing import BudgetExceeded, SpendLedger, load_pricing, preview_run
from nraiorbit.pipeline import Pipeline
from nraiorbit.post import ffmpeg
from nraiorbit.providers import google_ai
from nraiorbit.schemas import ScriptBlock

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
pytest.importorskip("google.genai")

LEAD = {"name": "Kaasi (voice artist K)", "gender": "male", "gemini_voice": "Orus", "dialect": "godavari"}


class FakeTTSClient:
    def __init__(self):
        t = np.arange(24000) / 24000
        self.pcm = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes()
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._content)

    def _content(self, *, model, contents, config):
        voice = config.speech_config.voice_config.prebuilt_voice_config.voice_name
        self.calls.append({"voice": voice, "contents": contents})
        part = SimpleNamespace(inline_data=SimpleNamespace(data=self.pcm, mime_type="audio/L16;codec=pcm;rate=24000"))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")],
                               prompt_feedback=None)


def _line(block_id="sc012_b007"):
    return ScriptBlock.model_validate({
        "block_id": block_id, "kind": "dialogue", "character_id": "kaasi", "text_te": "జై భైరవ! జై భైరవ!",
        "text_en": "Hail Bhairava!", "delivery": {"rasa": "raudra", "intensity": 8, "pace": "medium",
                                                 "volume": "shout", "notes": "the whole crowd"}})


def _tts(tmp_path, fake, budget=None):
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", budget)
    voices = {"voices/kaasi_voice@v1": LEAD}
    return google_ai.GeminiTTS(voices, {"google": {}, "voice": {"max_pause_s": None}}, {}, ledger, client=fake), ledger


def test_crowd_line_is_a_chant_of_mixed_voices_each_billed_and_cached(tmp_path):
    fake = FakeTTSClient()
    tts, ledger = _tts(tmp_path, fake)
    out = tmp_path / "05_dialogue" / "sc012_b007" / "crowd6_take_1.wav"
    seconds = tts.synthesize_crowd(_line(), "voices/kaasi_voice@v1", out, 6)

    voices = [c["voice"] for c in fake.calls]
    assert len(voices) == 6 and voices[0] == "Orus" and len(set(voices)) == 6     # the character's own voice leads
    others = voices[1:]
    assert {v in google_ai.GEMINI_FEMALE_VOICES for v in others} == {True, False}  # mixed gender
    assert all(c["contents"].endswith("#### TRANSCRIPT\n" + _line().text_te) for c in fake.calls)   # same text
    assert fake.calls[0]["contents"].startswith("# AUDIO PROFILE: Kaasi")
    records = [r for r in ledger.records() if r["provider"] == "gemini_tts"]
    assert len(records) == 6 and records[0]["item"] == "sc012_b007"                # every call billed

    assert 1.0 <= seconds <= 1.25                                                  # staggered by at most 150 ms
    assert ffmpeg.duration(out) == pytest.approx(seconds, abs=0.01)
    assert not (out.parent / "crowd6_take_1_crowd").exists()                       # the parts are cleaned up
    pcm = np.frombuffer(_read_pcm(out), "<i2").astype(float) / 32768
    assert 0.02 < np.sqrt(np.mean(pcm ** 2)) and np.abs(pcm).max() < 0.95          # loudness-normalised, no clipping

    again = tts.synthesize_crowd(_line(), "voices/kaasi_voice@v1", tmp_path / "again.wav", 6)
    assert len(fake.calls) == 6 and again == pytest.approx(seconds, abs=1e-3)      # cached like any take
    assert len([r for r in ledger.records() if r["provider"] == "gemini_tts"]) == 6
    single = tts.synthesize(_line(), "voices/kaasi_voice@v1", tmp_path / "single.wav")
    assert len(fake.calls) == 6 and single == pytest.approx(1.0, abs=0.02)         # the lead's take is shared


def test_crowd_choice_is_stable_and_never_repeats_the_lead():
    import random

    picks = google_ai.crowd_voices("Kore", 7, random.Random("x"))
    assert picks == google_ai.crowd_voices("Kore", 7, random.Random("x"))
    assert "Kore" not in [v for v, _ in picks] and picks[0][1] == "male"
    assert [g for _, g in picks] == ["male", "female"] * 3 + ["male"]


def test_crowd_calls_are_budget_checked(tmp_path):
    fake = FakeTTSClient()
    per_call = 0.02 / 60                                              # one second of speech at $0.02/min
    budget = 0.02 * 0.25 + 2.5 * per_call                             # each call checks for a 15 s reserve first
    tts, ledger = _tts(tmp_path, fake, budget=budget)
    with pytest.raises(BudgetExceeded):
        tts.synthesize_crowd(_line(), "voices/kaasi_voice@v1", tmp_path / "take.wav", 6)
    assert len(fake.calls) == 3 and ledger.spent() <= budget          # stopped before the fourth voice


def _read_pcm(path):
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())


class RecordingTTS:
    def __init__(self):
        self.calls = []

    def synthesize(self, line, voice, out):
        self.calls.append(("single", line.block_id, voice))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return 1.0

    def synthesize_crowd(self, line, voice, out, voices):
        self.calls.append(("crowd", line.block_id, voice, voices))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return 1.2


class NoGuard:
    def check(self, *a, **k):
        raise AssertionError("the voice guard must not judge a crowd take")


def test_pipeline_voices_crowd_lines_without_the_voice_guard(tmp_path):
    cfg = {"voice": {"takes_per_line": 3, "crowd": {"sc012_b007": {"voices": 6}}}}
    host = SimpleNamespace(cfg=cfg, p=SimpleNamespace(dialogue=tmp_path / "05_dialogue"), log=lambda m: None)
    host._crowd_voices = lambda bid: Pipeline._crowd_voices(host, bid)
    tts = RecordingTTS()
    wav, dur = Pipeline._render_line(host, tts, _line(), "voices/kaasi_voice@v1", NoGuard(), None)
    assert tts.calls == [("crowd", "sc012_b007", "voices/kaasi_voice@v1", 6)]
    assert wav == tmp_path / "05_dialogue" / "sc012_b007" / "crowd6_take_1.wav" and dur == 1.2

    tts.calls.clear()
    Pipeline._render_line(host, tts, _line("sc012_b008"), "voices/kaasi_voice@v1", None, None)
    assert tts.calls == [("single", "sc012_b008", "voices/kaasi_voice@v1")]        # other lines: one voice


def test_inbox_with_a_mock_fallback_says_a_crowd_line_with_the_single_voice(tmp_path):
    from nraiorbit.providers.inbox import InboxTTS

    logs = []
    cfg = {"voice": {"takes_per_line": 1, "crowd": {"sc012_b007": {"voices": 6}}}}
    host = SimpleNamespace(cfg=cfg, p=SimpleNamespace(dialogue=tmp_path / "05_dialogue"), log=logs.append)
    host._crowd_voices = lambda bid: Pipeline._crowd_voices(host, bid)
    tts = InboxTTS(tmp_path, fallback="mock")                 # no recording in inbox/dialogue, and mock cannot chant
    wav, _ = Pipeline._render_line(host, tts, _line(), "voices/kaasi_voice@v1", None, None)
    assert wav == tmp_path / "05_dialogue" / "sc012_b007" / "take_1.wav" and wav.exists()
    assert any("cannot voice a crowd" in m for m in logs)

    recording = tmp_path / "inbox" / "dialogue" / "sc012_b007.wav"     # a recorded crowd is used as the chant
    recording.parent.mkdir(parents=True)
    ffmpeg.run(["-f", "lavfi", "-i", "sine=frequency=220:duration=1", str(recording)])
    logs.clear()
    wav, _ = Pipeline._render_line(host, tts, _line(), "voices/kaasi_voice@v1", NoGuard(), None)
    assert wav.name == "crowd6_take_1.wav" and wav.exists() and not logs


def test_estimate_counts_one_tts_call_per_crowd_voice(tmp_path):
    from nraiorbit.costing import estimate
    from nraiorbit.poc import create_poc
    from nraiorbit.schemas import SceneScript

    root = create_poc(tmp_path / "godavari5")
    script = SceneScript.model_validate_json(sorted((root / "03_scenes").glob("*.json"))[0].read_text())
    block = next(b for b in script.blocks if b.kind == "dialogue")
    cfg = deep_merge(load_pipeline_config(root), {"providers": {"tts": "gemini"}})
    plain = estimate(root, cfg=cfg)
    crowd = estimate(root, cfg=deep_merge(cfg, {"voice": {"crowd": {block.block_id: {"voices": 6}}}}))
    assert plain.stats.from_plan and plain.tts["low"] > 0
    assert crowd.tts["low"] == pytest.approx(plain.tts["low"] * (plain.stats.lines + 5) / plain.stats.lines)


def test_run_preview_counts_one_tts_call_per_crowd_voice(tmp_path):
    from nraiorbit.poc import create_poc
    from nraiorbit.schemas import SceneScript

    root = create_poc(tmp_path / "godavari5")
    script = SceneScript.model_validate_json(sorted((root / "03_scenes").glob("*.json"))[0].read_text())
    block = next(b for b in script.blocks if b.kind == "dialogue")
    cfg = deep_merge(load_pipeline_config(root), {"providers": {"tts": "gemini"}})
    plain = preview_run(root, cfg, load_pricing(root))["lines"]
    crowd = deep_merge(cfg, {"voice": {"crowd": {block.block_id: {"voices": 6}}}})
    assert preview_run(root, crowd, load_pricing(root))["lines"] == plain + 5
