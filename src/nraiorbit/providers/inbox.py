"""Import media made outside the pipeline: Veo clips, recorded or external voices, music.

Each provider looks in projects/<slug>/inbox/<kind>/ and conforms what it finds to the
timeline (size, frame rate, length). Anything missing falls back to a placeholder, so a
half-finished project still assembles. The inbox lives outside the generated folders,
so `--force render` never deletes your files.
"""

from __future__ import annotations

from pathlib import Path

from ..post import ffmpeg
from ..schemas import Cue, ScriptBlock, Shot
from ..shotpack import clips_for, inbox_dir
from .base import RenderContext, get_provider, register
from .mock import MockMusic, MockVideo

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".aiff")


@register("video", "shotpack")
class ShotPackVideo:
    """Attempt N of a shot = the N-th clip in inbox/video (<shot>.mp4, <shot>_2.mp4, ...)."""

    def __init__(self, project_root: Path):
        self.root = project_root
        self.placeholder = MockVideo()

    def render_shot(self, shot: Shot, out: Path, ctx: RenderContext) -> Path:
        clips = clips_for(self.root, shot.shot_id)
        attempt = max(1, ctx.seed)
        if attempt > len(clips):
            return self.placeholder.render_shot(shot, out, ctx, note="MISSING: generate in Veo")
        return ffmpeg.conform_clip(clips[attempt - 1], out, shot.duration_s, ctx.resolution, ctx.fps)


def _find_audio(folder: Path, stem: str) -> Path | None:
    return next((folder / f"{stem}{ext}" for ext in AUDIO_EXTS if (folder / f"{stem}{ext}").exists()), None)


@register("tts", "inbox")
class InboxTTS:
    """inbox/dialogue/<line_id>.wav (a voice artist's recording or any external TTS); else the fallback provider."""

    def __init__(self, project_root: Path, fallback: str = "mock", **fallback_kwargs):
        self.folder = inbox_dir(project_root, "dialogue")
        self.fallback = get_provider("tts", fallback, **fallback_kwargs)

    def synthesize(self, line: ScriptBlock, voice_ref: str | None, out: Path) -> float:
        src = _find_audio(self.folder, line.block_id)
        if src is None:
            return self.fallback.synthesize(line, voice_ref, out)
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.run(["-i", str(src), "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(out)])
        return ffmpeg.duration(out)

    def can_chant(self, line: ScriptBlock) -> bool:
        """A crowd recording in the inbox, or a fallback that can chant (the pipeline says the single voice otherwise)."""
        return _find_audio(self.folder, line.block_id) is not None or hasattr(self.fallback, "synthesize_crowd")

    def synthesize_crowd(self, line: ScriptBlock, voice_ref: str | None, out: Path, voices: int) -> float:
        """A recording in the inbox is used as it is; else the fallback chants it."""
        if _find_audio(self.folder, line.block_id) is None:
            return self.fallback.synthesize_crowd(line, voice_ref, out, voices)
        return self.synthesize(line, voice_ref, out)


@register("music", "inbox")
class InboxMusic:
    """inbox/music/<cue_id>.wav, trimmed or padded to the cue and faded out; else a placeholder tone."""

    def __init__(self, project_root: Path):
        self.folder = inbox_dir(project_root, "music")
        self.placeholder = MockMusic()

    def render_cue(self, cue: Cue, out: Path) -> Path:
        src = _find_audio(self.folder, cue.cue_id)
        if src is None:
            return self.placeholder.render_cue(cue, out)
        dur = max(0.5, cue.duration_s - cue.silence_before_s)
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.run(["-i", str(src), "-af", f"apad,atrim=0:{dur:.3f},afade=t=out:st={max(0.0, dur - 1.5):.3f}:d=1.5",
                    "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(out)])
        return out
