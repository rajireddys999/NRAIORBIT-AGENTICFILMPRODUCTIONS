"""Capability interfaces. Each generation engine is one adapter behind one of these.

Production adapters (ComfyUI workflows, commercial APIs, self-hosted TTS/music) register
under a name and are selected in config/pipeline.yaml -> providers. See docs/TOOLS.md
for candidate models per capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ..schemas import CastLock, Cue, ScriptBlock, Shot


@dataclass
class RenderContext:
    """Everything an adapter may need beyond the shot itself."""

    cast: CastLock
    scene_id: str
    resolution: tuple[int, int]
    fps: int
    keyframe: Path | None = None
    seed: int = 0
    extra: dict = field(default_factory=dict)


class KeyframeProvider(Protocol):
    def render_keyframes(self, shot: Shot, out_dir: Path, n: int, ctx: RenderContext) -> list[Path]: ...


class VideoProvider(Protocol):
    def render_shot(self, shot: Shot, out: Path, ctx: RenderContext) -> Path: ...


class TTSProvider(Protocol):
    def synthesize(self, line: ScriptBlock, voice_ref: str | None, out: Path) -> float:
        """Write a WAV for the line and return its duration in seconds."""
        ...


class LipSyncProvider(Protocol):
    def apply(self, video: Path, audio: Path, out: Path) -> Path: ...


class MusicProvider(Protocol):
    def render_cue(self, cue: Cue, out: Path) -> Path: ...


_REGISTRY: dict[tuple[str, str], Callable[..., object]] = {}


def register(capability: str, name: str):
    def deco(factory):
        _REGISTRY[(capability, name)] = factory
        return factory
    return deco


def get_provider(capability: str, name: str, **kwargs):
    if name in (None, "none"):
        return None
    try:
        factory = _REGISTRY[(capability, name)]
    except KeyError:
        available = sorted(n for c, n in _REGISTRY if c == capability)
        raise KeyError(f"no '{capability}' provider named '{name}'. Registered: {available}") from None
    return factory(**kwargs)
