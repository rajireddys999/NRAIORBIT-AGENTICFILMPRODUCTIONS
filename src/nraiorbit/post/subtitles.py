"""SRT subtitle tracks from placed dialogue (Telugu + English)."""

from __future__ import annotations

from pathlib import Path


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(entries: list[tuple[float, float, str]], out: Path) -> Path:
    """entries: (start_s, end_s, text), in any order."""
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (start, end, text) in enumerate(sorted(entries), 1):
        lines += [str(i), f"{_ts(start)} --> {_ts(end)}", text.strip(), ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
