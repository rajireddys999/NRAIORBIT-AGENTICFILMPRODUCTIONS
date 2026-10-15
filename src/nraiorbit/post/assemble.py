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


def frame_grid(shots: list[TimelineShot], fps: int) -> list[int]:
    """Frames per shot so that every cut sits on the film's frame grid: shot i fills frames round(start * fps) up to
    round(end * fps). Each clip is rounded to whole frames on its own (4.8 s at 24 fps is 115 frames, not 115.2), and
    those rounding errors add up: the dialogue sits at exact times, so by the last reel the voice was 92 ms behind the
    lips. On the grid the picture is never more than half a frame from the sound."""
    if not shots:
        return []
    edges = [s.start_s for s in shots] + [shots[-1].start_s + shots[-1].duration_s]
    marks = [int(round(t * fps)) for t in edges]
    return [max(1, b - a) for a, b in zip(marks, marks[1:])]


def fit_frames(clip: Path, frames: int, out: Path) -> Path:
    """`clip` with exactly `frames` frames: its tail trimmed or its last frame held (a frame or so at a cut). Clips
    that already fit are used as they are."""
    have = ffmpeg.frame_count(clip)
    if have == frames:
        return clip
    hold = f"tpad=stop_mode=clone:stop={frames - have}" if frames > have else "null"
    ffmpeg.run(["-i", str(clip), "-vf", hold, "-frames:v", str(frames), "-an", *ffmpeg.VIDEO_ENCODE, str(out)])
    return out


def render_scene(shots: list[TimelineShot], dialogue: list[AudioEvent], scene_start_s: float, out: Path,
                 fps: int = 24) -> Path:
    """dialogue: every line overlapping the scene. Its audio is exactly as long as its video; a line crossing either
    edge is cut there and carries on in the neighbouring scene (see join_scenes). Each shot's picture is fitted to
    the film's frame grid (see frame_grid), so the lips stay on the voice however long the film runs."""
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    grid = frame_grid(shots, fps)
    clips = [fit_frames(s.clip, n, work / f"fit_{i:03d}.mp4") for i, (s, n) in enumerate(zip(shots, grid))]
    video = ffmpeg.concat(clips, work / "video.mp4")
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
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest",
                "-ar", "48000", str(out)])
    return out


TITLE_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"]
TITLE_GOLD = "0xE8B44C"


def _title_card(source: Path, dur: float, w: int, h: int, fps: int, title: str, work: Path,
                image: Path | None, backdrop_s: float | None) -> tuple[list[str], str]:
    """Inputs and filter for a cinematic title card: a designed title image with a slow push-in, or the title in gold
    over a dark, blurred frame of the film (black if none), always faded in and out. Never plain text on black."""
    n = max(1, int(round(dur * fps)))
    fade = f"fade=t=in:st=0:d={min(0.35, dur / 4):.3f},fade=t=out:st={max(0.0, dur - 0.45):.3f}:d={min(0.45, dur / 3):.3f}"
    if image is not None and image.exists():
        return (["-loop", "1", "-framerate", str(fps), "-i", str(image)],
                f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
                f"zoompan=z='1+0.06*on/{n}':d={n}:s={w}x{h}:fps={fps},{fade}")
    frame = work / "title_backdrop.png"
    if backdrop_s is not None:                        # one frame of the film, as a still: an exact frame count after
        ffmpeg.run(["-ss", f"{backdrop_s:.3f}", "-i", str(source), "-frames:v", "1", "-update", "1", str(frame)])
    if backdrop_s is not None and frame.exists():
        inputs = ["-loop", "1", "-framerate", str(fps), "-i", str(frame)]
        still = f"scale={w}:{h},boxblur=lr={max(8, w // 80)}:lp=2,eq=brightness=-0.32:saturation=0.6,format=yuv420p"
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}"]
        still = "format=yuv420p"
    text = ""
    font = next((f for f in TITLE_FONTS if Path(f).exists()), None) or ffmpeg.font_file()
    if font and title:
        label = work / "title.txt"
        label.write_text(title.upper(), encoding="utf-8")
        text = (f",drawtext=fontfile={font}:textfile={label}:fontcolor={TITLE_GOLD}:fontsize={h // 8}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:shadowcolor=black@0.85:shadowx={max(2, h // 270)}:"
                f"shadowy={max(2, h // 270)}:borderw={max(1, h // 540)}:bordercolor=0x5A3A0A")
    return inputs, f"{still}{text},{fade}"


def cut_segments(source: Path, segments: list[tuple[float | None, float]], out: Path, title: str = "",
                 resolution: tuple[int, int] = (640, 360), fps: int = 24, audio: bool = True,
                 title_image: Path | None = None, title_backdrop_s: float | None = None) -> Path:
    """Cut (start_s, duration_s) segments from `source` and join them in order, each in whole frames.

    A segment with start None becomes the title card: `title_image` (a designed title) if given, else `title` in gold
    over a blurred, darkened frame of the film at `title_backdrop_s`. audio=False cuts picture only (the trailer lays
    whole dialogue lines over it instead of the film's sound).
    """
    work = out.parent / f".{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    w, h = resolution
    encode = [*ffmpeg.VIDEO_ENCODE, "-r", str(fps), *(["-c:a", "aac", "-ar", "48000", "-ac", "2"] if audio else ["-an"])]
    parts: list[Path] = []
    for i, (start, dur) in enumerate(segments):
        part = work / f"seg_{i:03d}.mp4"
        frames = ["-frames:v", str(max(1, int(round(dur * fps))))]
        if start is None:
            inputs, vf = _title_card(source, dur, w, h, fps, title, work, title_image, title_backdrop_s)
            silence = ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"] if audio else []
            ffmpeg.run([*inputs, *silence, "-vf", vf, "-map", "0:v", *(["-map", "1:a"] if audio else []),
                        *encode, *frames, str(part)])
        else:
            ffmpeg.run(["-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
                        "-vf", f"scale={w}:{h}", *encode, *frames, str(part)])
        parts.append(part)
    return ffmpeg.concat(parts, out)
