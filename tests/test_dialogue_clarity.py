"""Dialogue levelling and the clarity pass: every line ends up clearly above the music, without stems."""

import json
import subprocess
import wave

import numpy as np
import pytest

from nraiorbit.post import ffmpeg
from nraiorbit.post.speech import clarify_dialogue, level_gain_db, speech_level

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")

RATE = 48000


def _write(path, x, rate=RATE):
    x = np.atleast_2d(x.T).T if x.ndim == 1 else x
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _voice(seconds, rms_db, seed):
    """Speech-like: a 140 Hz harmonic stack, syllable-rate amplitude modulation, at rms_db dBFS."""
    t = np.arange(int(seconds * RATE)) / RATE
    rng = np.random.default_rng(seed)
    x = sum(np.sin(2 * np.pi * 140 * k * t + rng.uniform(0, 6.28)) / k for k in range(1, 12))
    x *= 0.55 + 0.45 * np.sin(2 * np.pi * 4.2 * t)
    return x / np.sqrt(np.mean(x ** 2)) * 10 ** (rms_db / 20)


def _music(seconds, rms_db, seed=7):
    x = np.random.default_rng(seed).standard_normal(int(seconds * RATE))
    x = np.convolve(x, np.ones(8) / 8, mode="same")              # duller than white noise, like a score bed
    return x / np.sqrt(np.mean(x ** 2)) * 10 ** (rms_db / 20)


def _lufs(path):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(err.rsplit("I:", 1)[1].split("LUFS")[0])


def _over(mix_path, take_path, start_s):
    """Voice minus music (dB) for a known take in a mix, measured like the clarity pass does."""
    rep = clarify_dialogue(mix_path, [(take_path, start_s, 0.0)], mix_path.parent / "probe.wav", min_db=-99)
    return rep[0]["over_music_db"]


def test_speech_level_ignores_pauses(tmp_path):
    v = _voice(1.0, -23.0, 1)
    x = np.concatenate([np.zeros(RATE), v, np.zeros(2 * RATE)])
    level = speech_level(_write(tmp_path / "take.wav", x))
    assert level == pytest.approx(-23.0, abs=1.0)                 # the 3 s of silence do not drag it down
    assert speech_level(_write(tmp_path / "silent.wav", np.zeros(RATE))) is None


def test_level_gain_is_clamped(tmp_path):
    quiet = _write(tmp_path / "quiet.wav", _voice(1.0, -45.0, 2))
    loud = _write(tmp_path / "loud.wav", _voice(1.0, -3.0, 3))
    assert level_gain_db(quiet, -20.0) == pytest.approx(12.0)      # at most +12 dB
    assert level_gain_db(loud, -20.0) == pytest.approx(-6.0)       # at most -6 dB
    mid = _write(tmp_path / "mid.wav", _voice(1.0, -24.0, 4))
    assert level_gain_db(mid, -20.0) == pytest.approx(4.0, abs=1.0)


def test_clarity_lifts_only_buried_lines(tmp_path):
    buried, clear = _voice(1.2, -38.0, 5), _voice(1.2, -14.0, 6)   # 10 dB under the music / 14 dB over it
    music = _music(6.0, -28.0)                                       # a mastered mix: peaks well under -1 dB
    mix = music.copy()
    mix[2 * RATE: 2 * RATE + len(buried)] += buried
    mix[4 * RATE: 4 * RATE + len(clear)] += clear
    src = _write(tmp_path / "mix.wav", np.stack([mix, mix], axis=1))
    a, b = _write(tmp_path / "a.wav", buried), _write(tmp_path / "b.wav", clear)

    out = tmp_path / "clear.wav"
    report = clarify_dialogue(src, [(a, 2.04, 0.0), (b, 3.97, 0.0)], out, min_db=12, max_boost_db=12)
    first, second = report
    assert first["start_s"] == pytest.approx(2.0, abs=0.002)        # found to the sample though 40 ms off
    assert first["over_music_db"] == pytest.approx(-10.0, abs=1.0)
    assert first["boost_db"] == pytest.approx(12.0)                 # capped at +12 dB
    assert second["over_music_db"] == pytest.approx(14.0, abs=1.0) and second["boost_db"] == 0

    assert _over(out, a, 2.0) == pytest.approx(2.0, abs=1.5)        # -10 dB + 12 dB
    assert _over(out, b, 4.0) == pytest.approx(14.0, abs=1.5)       # untouched
    assert ffmpeg.duration(out) == pytest.approx(6.0, abs=0.01)
    assert _lufs(out) == pytest.approx(_lufs(src), abs=0.3)         # the mix keeps its loudness
    peak = np.abs(np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "f32le", "-"],
                                               capture_output=True).stdout, "<f4")).max()
    assert peak <= 10 ** (-1.0 / 20) + 0.01                          # limited at -1 dB


def test_pipeline_mix_report_and_levelled_lines(tmp_path):
    """The demo pipeline writes a mix report for the film and the trailer, one record per placed line."""
    import copy

    from nraiorbit.config import deep_merge, load_pipeline_config
    from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
    from nraiorbit.pipeline import Pipeline

    root = tmp_path / "godavari"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = deep_merge(load_pipeline_config(), copy.deepcopy(DEMO_CONFIG))
    Pipeline(root, demo_llm(), cfg=cfg, log=lambda m: None).run()
    records = [json.loads(line) for line in (root / "reports" / "mix.jsonl").read_text().splitlines()]
    film = next(r for r in records if r["master"] == "film")
    timeline = json.loads((root / "10_edit" / "timeline.json").read_text())
    assert film["min_voice_over_music_db"] == 12 and len(film["lines"]) == len(timeline["dialogue"])
    assert all("over_music_db" in r and "boost_db" in r for r in film["lines"])
    assert any(r["master"] == "trailer" for r in records)
