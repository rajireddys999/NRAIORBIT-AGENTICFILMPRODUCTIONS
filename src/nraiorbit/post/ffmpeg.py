"""Thin ffmpeg/ffprobe helpers shared by mock providers and assembly."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

SAMPLE_RATE = 48000
# Every clip that is later joined with stream copy must share these settings, or players
# (QuickTime, Resolve) can mis-decode the segments after a join.
VIDEO_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p"]
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",      # macOS
    "/System/Library/Fonts/Helvetica.ttc",               # macOS
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


class FFmpegError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(args: list[str]) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


def font_file() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def concat(paths: list[Path], out: Path) -> Path:
    """Concatenate media with identical encoding parameters (stream copy)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".concat.txt")
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in paths), encoding="utf-8")
    run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)])
    listing.unlink()
    return out


def place_audio(events: list[tuple[Path, float, float]], total_s: float, out: Path) -> Path:
    """Mix audio files at offsets onto a silent bed. events: (path, start_s, gain_db)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["-f", "lavfi", "-t", f"{total_s:.3f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
    filters, labels = [], ["[0:a]"]
    for i, (path, start, gain_db) in enumerate(events, 1):
        inputs += ["-i", str(path)]
        ms = max(0, int(round(start * 1000)))
        filters.append(
            f"[{i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
            f"volume={gain_db}dB,adelay=delays={ms}:all=1[a{i}]"
        )
        labels.append(f"[a{i}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[out]")
    script = out.with_suffix(".filter.txt")
    script.write_text(";\n".join(filters), encoding="utf-8")
    run([*inputs, "-filter_complex_script", str(script), "-map", "[out]", "-ar", str(SAMPLE_RATE), str(out)])
    script.unlink()
    return out


def mux(video: Path, audio: Path, out: Path) -> Path:
    run(["-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])
    return out


def extract_frames(video: Path, every_n: int, out_dir: Path) -> list[Path]:
    """Every n-th frame plus the last frame, as PNGs (for the identity guard)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["-i", str(video), "-vf", f"select=not(mod(n\\,{every_n}))", "-fps_mode", "vfr",
         str(out_dir / "f_%05d.png")])
    run(["-sseof", "-0.1", "-i", str(video), "-frames:v", "1", "-update", "1", str(out_dir / "last.png")])
    return sorted(out_dir.glob("*.png"))


def conform_clip(src: Path, out: Path, duration_s: float, resolution: tuple[int, int], fps: int) -> Path:
    """Fit an external clip to its slot: scale/pad to the frame, set fps, trim or hold the last frame, drop audio."""
    w, h = resolution
    out.parent.mkdir(parents=True, exist_ok=True)
    run([
        "-i", str(src), "-t", f"{duration_s:.3f}", "-an",
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
               f"setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={duration_s:.3f}",
        "-frames:v", str(int(round(duration_s * fps))), *VIDEO_ENCODE, str(out),
    ])
    return out
