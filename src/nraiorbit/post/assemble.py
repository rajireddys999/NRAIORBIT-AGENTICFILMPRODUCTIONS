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
    """dialogue: every line overlapping the scene. Its audio is exactly as long as its video; a line crossing either
    edge is cut there and carries on in the neighbouring scene (see join_scenes)."""
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    video = ffmpeg.concat([s.clip for s in shots], work / "video.mp4")
    total = sum(s.duration_s for s in shots)
    events = [(e.path, e.start_s - scene_start_s, e.gain_db) for e in dialogue]
    audio = ffmpeg.place_audio(events, total, work / "dialogue.wav")
    ffmpeg.mux(video, audio, out)
    return out


def join_scenes(scenes: list[Path], out: Path) -> Path:
    """Join rendered scenes: picture by stream copy, dialogue from the scenes' sample-exact WAVs, encoded once.

    Joining the scenes' AAC tracks instead would leave encoder priming and padding at every cut, a gap or click in
    the middle of a line that crosses it.
    """
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    parts = [s.parent / f".{s.stem}" for s in scenes]
    video = ffmpeg.concat([p / "video.mp4" for p in parts], work / "video.mp4")
    audio = ffmpeg.concat([p / "dialogue.wav" for p in parts], work / "dialogue.wav")
    return ffmpeg.mux(video, audio, out)


def _dip_gain(dips: list[tuple[float, float, float]], ramp_s: float = 0.05) -> str:
    """volume expression: each (start_s, duration_s, gain_db) dip ramps down and back up over ramp_s."""
    return "*".join(f"(1-{1 - 10 ** (db / 20):.4f}*clip(min((t-{a:.3f})/{ramp_s},({a + d:.3f}-t)/{ramp_s}),0,1))"
                    for a, d, db in dips)


def master(film_with_dialogue: Path, music_events: list[AudioEvent], out: Path,
           lufs: float = -14.0, true_peak: float = -1.0, music_dips: list[tuple[float, float, float]] = ()) -> Path:
    """Lay music under the dialogue film, duck it with a sidechain, normalise loudness.

    music_dips: (start_s, duration_s, gain_db) volume automation on the music bed, e.g. the drop before a punch line.
    """
    total = ffmpeg.duration(film_with_dialogue)
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    music = ffmpeg.place_audio([(e.path, e.start_s, e.gain_db) for e in music_events], total, work / "music.wav")
    bed = f"[1:a]asetnsamples=n=240,volume='{_dip_gain(list(music_dips))}':eval=frame[bed];[bed]" if music_dips else "[1:a]"
    graph = (
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[dlg][sc];"
        f"{bed}[sc]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400[ducked];"
        f"[dlg][ducked]amix=inputs=2:duration=first:normalize=0,"
        f"loudnorm=I={lufs}:TP={true_peak}:LRA=11[aout]"
    )
    ffmpeg.run(["-i", str(film_with_dialogue), "-i", str(music), "-filter_complex", graph,
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                "-ar", "48000", str(out)])
    return out


def cut_segments(source: Path, segments: list[tuple[float | None, float]], out: Path, title: str = "",
                 resolution: tuple[int, int] = (640, 360), fps: int = 24, audio: bool = True) -> Path:
    """Cut (start_s, duration_s) segments from `source` and join them in order, each in whole frames.

    A segment with start None becomes a black title card showing `title`. audio=False cuts picture only (the
    trailer lays whole dialogue lines over it instead of the film's sound).
    """
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    w, h = resolution
    font = ffmpeg.font_file()
    encode = [*ffmpeg.VIDEO_ENCODE, "-r", str(fps), *(["-c:a", "aac", "-ar", "48000", "-ac", "2"] if audio else ["-an"])]
    parts: list[Path] = []
    for i, (start, dur) in enumerate(segments):
        part = work / f"seg_{i:03d}.mp4"
        frames = ["-frames:v", str(max(1, int(round(dur * fps))))]
        if start is None:
            vf = []
            if font and title:
                label = work / "title.txt"
                label.write_text(title, encoding="utf-8")
                vf = ["-vf", f"drawtext=fontfile={font}:textfile={label}:fontcolor=white:"
                             f"fontsize={h // 9}:x=(w-text_w)/2:y=(h-text_h)/2"]
            silence = ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"] if audio else []
            ffmpeg.run(["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={dur:.3f}",
                        *silence, *vf, *encode, *frames, str(part)])
        else:
            ffmpeg.run(["-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
                        "-vf", f"scale={w}:{h}", *encode, *frames, str(part)])
        parts.append(part)
    return ffmpeg.concat(parts, out)
