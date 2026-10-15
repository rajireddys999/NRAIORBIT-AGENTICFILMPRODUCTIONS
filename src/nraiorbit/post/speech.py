"""Clean-up for generated speech: trim the silence around a line and shorten long pauses inside it.

TTS models read a dramatic line with 0.5-1 s gaps between sentences, which sounds broken in a cut. Done in numpy
rather than with ffmpeg's silenceremove: that filter was rewritten in ffmpeg 6.1 (on 6.1.1, stop_periods=-1,
stop_duration=0.6, stop_silence=0.3 shortened a 1.1 s pause only to 0.9 s), and the same take must come out the same on
every machine.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def tighten_pauses(path: Path, max_pause_s: float = 0.6, keep_s: float = 0.3, threshold_db: float = -45.0,
                   edge_s: float = 0.06) -> float:
    """Rewrite a 16-bit WAV in place: leading/trailing silence trimmed to edge_s, every pause longer than max_pause_s
    shortened to keep_s (two thirds kept after the words, one third before the next). Returns the new duration."""
    with wave.open(str(path), "rb") as w:
        params = w.getparams()
        raw = w.readframes(params.nframes)
    rate, n = params.framerate, params.nframes
    if params.sampwidth != 2 or n == 0:
        return n / rate if rate else 0.0
    x = np.frombuffer(raw, "<i2").reshape(-1, params.nchannels)
    hop = max(1, rate // 100)                                            # 10 ms analysis frames
    frames = x[: n // hop * hop].astype(np.float32).reshape(-1, hop * params.nchannels) / 32768
    loud = np.flatnonzero(np.sqrt((frames ** 2).mean(axis=1)) > 10 ** (threshold_db / 20))
    if loud.size == 0:                                                   # all silence: leave it to the reviewer
        return n / rate
    edge = int(edge_s * rate)
    keep = np.zeros(n, bool)
    keep[max(0, loud[0] * hop - edge): min(n, (loud[-1] + 1) * hop + edge)] = True
    after, before = int(keep_s * rate * 2 / 3), int(keep_s * rate / 3)
    for g in np.flatnonzero(np.diff(loud) > 1):
        start, end = (loud[g] + 1) * hop, loud[g + 1] * hop             # a silent run between two loud frames
        if (end - start) / rate > max_pause_s:
            keep[start + after: end - before] = False
    if keep.all():
        return n / rate
    with wave.open(str(path), "wb") as w:
        w.setparams(params)
        w.writeframes(np.ascontiguousarray(x[keep]).tobytes())
    return int(keep.sum()) / rate


def _decode(path: Path, rate: int, channels: int) -> np.ndarray:
    """Any audio (or a video's audio) as float32 samples, shape (n, channels)."""
    import subprocess

    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", str(channels), "-ar", str(rate),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<f4").reshape(-1, channels).copy()


def speech_level(path: Path, rate: int = 16000, floor_db: float = 30.0) -> float | None:
    """Active speech level of a take in dBFS: the mean power of its 20 ms frames within floor_db of the loudest one,
    so pauses and room tone don't pull it down. None for a silent file."""
    x = _decode(path, rate, 1)[:, 0]
    hop = rate // 50
    frames = x[: len(x) // hop * hop].reshape(-1, hop)
    if frames.size == 0:
        return None
    power = (frames ** 2).mean(axis=1)
    if power.max() <= 1e-10:
        return None
    active = power[power >= power.max() * 10 ** (-floor_db / 10)]
    return float(10 * np.log10(active.mean()))


def level_gain_db(path: Path, target_dbfs: float, max_cut_db: float = 6.0, max_boost_db: float = 12.0) -> float:
    """Gain that brings a take's active speech level to target_dbfs, within [-max_cut_db, +max_boost_db]."""
    level = speech_level(path)
    if level is None:
        return 0.0
    return float(min(max_boost_db, max(-max_cut_db, target_dbfs - level)))


def _integrated_lufs(x: np.ndarray, rate: int) -> float:
    """EBU R128 integrated loudness of float samples (n, channels), measured by ffmpeg."""
    import subprocess

    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-f", "f32le", "-ar", str(rate), "-ac",
                          str(x.shape[1]), "-i", "-", "-af", "ebur128", "-f", "null", "-"],
                         input=x.astype("<f4").tobytes(), capture_output=True).stderr.decode(errors="replace")
    return float(err.rsplit("I:", 1)[1].split("LUFS")[0])


def clarify_dialogue(mix: Path, lines: list[tuple[Path, float, float]], out: Path, min_db: float = 12.0,
                     max_boost_db: float = 12.0, true_peak: float = -1.0, rate: int = 48000) -> list[dict]:
    """Make every line stand min_db above the music in a finished mix, without stems.

    lines: (take, start_s, gain_db) as placed. Each take is a known signal, so it is found in the mix to the sample
    (cross-correlation within +-100 ms of start_s), its gain there is measured, and what is left is the music. A line
    under min_db gets more of exactly that take added in phase (at most max_boost_db). The result keeps the mix's
    measured loudness exactly (one gain), with a peak limiter at true_peak. Writes `out` (WAV); one record per line."""
    import subprocess

    x = _decode(mix, rate, 2)
    before = _integrated_lufs(x, rate)
    mono = x.mean(axis=1)
    report = []
    for take, start_s, _ in lines:
        v = _decode(take, rate, 1)[:, 0]
        if not len(v) or not np.any(v):
            continue
        s0, w = int(round(start_s * rate)), rate // 10
        lo, hi = max(0, s0 - w), min(len(mono), s0 + w + len(v))
        seg = mono[lo:hi]
        if len(seg) < len(v):
            continue
        n = 1 << int(np.ceil(np.log2(len(seg) + len(v))))
        xc = np.fft.irfft(np.fft.rfft(seg, n) * np.conj(np.fft.rfft(v, n)), n)[: len(seg) - len(v) + 1]
        at = lo + int(np.argmax(xc))
        part = mono[at: at + len(v)]
        g = float(np.dot(part, v) / (np.dot(v, v) + 1e-12))
        voice = 10 * np.log10(np.mean((g * v) ** 2) + 1e-12)
        music = 10 * np.log10(np.mean((part - g * v) ** 2) + 1e-12)
        over, boost = voice - music, 0.0
        if over < min_db and g > 0:
            boost = min(min_db - over, max_boost_db)
            x[at: at + len(v)] += ((10 ** (boost / 20) - 1) * g * v)[:, None]
        report.append({"take": str(take), "start_s": round(at / rate, 3), "over_music_db": round(float(over), 1),
                       "boost_db": round(float(boost), 1)})
    if any(r["boost_db"] for r in report):
        x *= 10 ** ((before - _integrated_lufs(x, rate)) / 20)
    out.parent.mkdir(parents=True, exist_ok=True)
    limit = min(1.0, max(0.0625, 10 ** (true_peak / 20)))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "2", "-i", "-",
                    "-af", f"alimiter=limit={limit:.4f}:level=disabled", "-ar", str(rate), "-c:a", "pcm_s16le",
                    str(out)], input=x.astype("<f4").tobytes(), check=True)
    return report
