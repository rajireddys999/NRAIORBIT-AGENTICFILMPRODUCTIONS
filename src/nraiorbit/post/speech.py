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
