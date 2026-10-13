"""Edit assembly and mix: scene renders -> film -> music bed with ducking -> loudness master.

Scenes are rendered independently (video concat + dialogue placement) and then
concatenated, which keeps every ffmpeg graph small even for a 1,500-shot feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg


@dataclass
class TimelineShot:
    shot_id: str
    scene_id: str
    start_s: float          # position in the film
    duration_s: float
    clip: Path
    real: bool = True       # False = placeholder card (not generated yet, or blocked)


@dataclass
class AudioEvent:
    path: Path
    start_s: float          # position in the film
    duration_s: float
    track: str              # dialogue | music | sfx
    gain_db: float = 0.0
    ref_id: str = ""        # line id / cue id


def render_scene(shots: list[TimelineShot], dialogue: list[AudioEvent], scene_start_s: float, out: Path) -> Path:
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    video = ffmpeg.concat([s.clip for s in shots], work / "video.mp4")
    total = sum(s.duration_s for s in shots)
    events = [(e.path, e.start_s - scene_start_s, e.gain_db) for e in dialogue]
    audio = ffmpeg.place_audio(events, total, work / "dialogue.wav")
    ffmpeg.mux(video, audio, out)
    return out


def master(film_with_dialogue: Path, music_events: list[AudioEvent], out: Path,
           lufs: float = -14.0, true_peak: float = -1.0) -> Path:
    """Lay music under the dialogue film, duck it with a sidechain, normalise loudness."""
    total = ffmpeg.duration(film_with_dialogue)
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    music = ffmpeg.place_audio([(e.path, e.start_s, e.gain_db) for e in music_events], total, work / "music.wav")
    graph = (
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[dlg][sc];"
        "[1:a][sc]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400[ducked];"
        f"[dlg][ducked]amix=inputs=2:duration=first:normalize=0,"
        f"loudnorm=I={lufs}:TP={true_peak}:LRA=11[aout]"
    )
    ffmpeg.run(["-i", str(film_with_dialogue), "-i", str(music), "-filter_complex", graph,
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                "-ar", "48000", str(out)])
    return out


def cut_segments(source: Path, segments: list[tuple[float | None, float]], out: Path, title: str = "",
                 resolution: tuple[int, int] = (640, 360), fps: int = 24) -> Path:
    """Cut (start_s, duration_s) segments from `source` and join them in order.

    A segment with start None becomes a black title card showing `title`.
    """
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    w, h = resolution
    font = ffmpeg.font_file()
    encode = [*ffmpeg.VIDEO_ENCODE, "-r", str(fps), "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    parts: list[Path] = []
    for i, (start, dur) in enumerate(segments):
        part = work / f"seg_{i:03d}.mp4"
        if start is None:
            vf = []
            if font and title:
                label = work / "title.txt"
                label.write_text(title, encoding="utf-8")
                vf = ["-vf", f"drawtext=fontfile={font}:textfile={label}:fontcolor=white:"
                             f"fontsize={h // 9}:x=(w-text_w)/2:y=(h-text_h)/2"]
            ffmpeg.run(["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={dur:.3f}",
                        "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                        *vf, *encode, "-shortest", str(part)])
        else:
            ffmpeg.run(["-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
                        "-vf", f"scale={w}:{h}", *encode, str(part)])
        parts.append(part)
    return ffmpeg.concat(parts, out)
