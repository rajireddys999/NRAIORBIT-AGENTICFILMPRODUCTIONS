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


def frame_count(path: Path) -> int:
    """Video frames in `path`: the container's count, or the packets counted when the container has none."""
    def probe(*args: str) -> dict:
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", *args, "-of", "json", str(path)],
                              capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)["streams"][0]
    n = str(probe("-show_entries", "stream=nb_frames").get("nb_frames", ""))
    return int(n) if n.isdigit() else int(probe("-count_packets", "-show_entries", "stream=nb_read_packets")
                                          ["nb_read_packets"])


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
    """Mix audio files at offsets onto a silent bed of exactly total_s. events: (path, start_s, gain_db).

    An event starting before 0 loses its head and one running past total_s is cut there, in whole samples, so a
    line that crosses a scene cut continues seamlessly in the next scene's audio.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    n = int(round(total_s * SAMPLE_RATE))
    inputs = ["-f", "lavfi", "-t", f"{n / SAMPLE_RATE:.6f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
    filters, labels = [], ["[0:a]"]
    for path, start, gain_db in events:
        offset = int(round(start * SAMPLE_RATE))
        if offset >= n:
            continue
        i = len(labels)
        inputs += ["-i", str(path)]
        place = (f"atrim=start_sample={-offset},asetpts=PTS-STARTPTS" if offset < 0
                 else f"adelay=delays={offset}S:all=1")
        filters.append(
            f"[{i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,volume={gain_db}dB,{place}[a{i}]")
        labels.append(f"[a{i}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0,"
                   f"apad=whole_len={n},atrim=end_sample={n}[out]")
    script = out.with_suffix(".filter.txt")
    script.write_text(";\n".join(filters), encoding="utf-8")
    run([*inputs, "-filter_complex_script", str(script), "-map", "[out]", "-ar", str(SAMPLE_RATE), str(out)])
    script.unlink()
    return out


def mux(video: Path, audio: Path, out: Path, bitrate: str = "192k") -> Path:
    """The picture (copied) with this sound. The picture sets the length: sound that comes out a little short (each
    AAC encode loses a few milliseconds at the end) is padded with silence, never allowed to cut frames."""
    run(["-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-af", "apad", "-c:a", "aac", "-b:a", bitrate, "-shortest", str(out)])
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
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
               f"setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={duration_s:.3f}",
        "-frames:v", str(int(round(duration_s * fps))), *VIDEO_ENCODE, str(out),
    ])
    return out
