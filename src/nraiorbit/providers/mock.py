"""Animatic providers: placeholder video and audio rendered by ffmpeg.

They let the whole pipeline run (and be reviewed for pacing) before any GPU is spent:
each shot becomes a colour card labelled with its id, size, rasa and characters; each
dialogue line a short tone whose length tracks the text; each cue a pulsing tone at the
cue's tempo.
"""

from __future__ import annotations

from pathlib import Path

from ..post import ffmpeg
from ..schemas import Cue, ScriptBlock, Shot
from .base import RenderContext, register

RASA_COLOURS = {
    "shringara": "0xC2185B", "hasya": "0xF9A825", "karuna": "0x37474F", "raudra": "0xB71C1C",
    "veera": "0xE65100", "bhayanaka": "0x1B5E20", "bibhatsa": "0x827717", "adbhuta": "0x4A148C",
    "shanta": "0x0277BD", "vatsalya": "0xAD1457", "bhakti": "0xFF8F00",
}
VOICE_PITCH_HZ = {"low": 140, "mid": 200, "high": 280}
RAGA_TONIC_HZ = 220.0


@register("video", "mock")
class MockVideo:
    def render_shot(self, shot: Shot, out: Path, ctx: RenderContext) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        w, h = ctx.resolution
        colour = RASA_COLOURS.get(shot.rasa, "0x333333")
        vf = []
        font = ffmpeg.font_file()
        if font:
            label = out.with_suffix(".label.txt")
            label.write_text(
                f"{shot.shot_id}  {shot.size}  {shot.camera_move}\n"
                f"{shot.rasa} {shot.intensity}/10  {', '.join(shot.characters_in_frame) or '-'}",
                encoding="utf-8",
            )
            vf = ["-vf", f"drawtext=fontfile={font}:textfile={label}:fontcolor=white:fontsize={h // 18}:"
                         f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=8"]
        ffmpeg.run([
            "-f", "lavfi", "-i", f"color=c={colour}:s={w}x{h}:r={ctx.fps}:d={shot.duration_s:.3f}",
            *vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out),
        ])
        if font:
            out.with_suffix(".label.txt").unlink()
        return out


@register("tts", "mock")
class MockTTS:
    """Tone per line. Duration ~ 11 Telugu graphemes per second, 0.8-8 s."""

    def __init__(self, pitch_by_voice: dict[str, str] | None = None):
        self.pitch_by_voice = pitch_by_voice or {}

    def synthesize(self, line: ScriptBlock, voice_ref: str | None, out: Path) -> float:
        out.parent.mkdir(parents=True, exist_ok=True)
        dur = min(8.0, max(0.8, len(line.text_te) / 11))
        hz = VOICE_PITCH_HZ.get(self.pitch_by_voice.get(voice_ref or "", "mid"), 200)
        ffmpeg.run([
            "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={dur:.3f}:sample_rate={ffmpeg.SAMPLE_RATE}",
            "-af", f"volume=0.4,afade=t=in:d=0.05,afade=t=out:st={max(0.0, dur - 0.1):.3f}:d=0.1",
            "-ac", "2", str(out),
        ])
        return dur


@register("music", "mock")
class MockMusic:
    """Tone pulsing at the cue tempo; brighter and louder with intensity."""

    def render_cue(self, cue: Cue, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        dur = max(0.5, cue.duration_s - cue.silence_before_s)
        hz = RAGA_TONIC_HZ * (1.5 if cue.intensity >= 7 else 1.0)
        gain = 0.08 + 0.015 * cue.intensity
        ffmpeg.run([
            "-f", "lavfi", "-i", f"sine=frequency={hz:.1f}:duration={dur:.3f}:sample_rate={ffmpeg.SAMPLE_RATE}",
            "-af", f"tremolo=f={cue.bpm / 60:.3f}:d=0.7,volume={gain:.3f},"
                   f"afade=t=in:d=0.5,afade=t=out:st={max(0.0, dur - 1.0):.3f}:d=1.0",
            "-ac", "2", str(out),
        ])
        return out
