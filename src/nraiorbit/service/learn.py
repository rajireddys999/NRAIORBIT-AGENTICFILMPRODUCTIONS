"""Reference learning (`nraiorbit learn youtube <url>…`): Gemini on Vertex AI watches a public film or trailer and
writes a profile of how it works, which the story, casting, music and trailer agents read as an example.

What a profile holds: how the score is built (cue anatomy, silence, instruments per rasa), how emotional beats are
paced, how action is staged and cut, the archetypes and screen presence of hero, heroine, villain and supporting
cast, how the trailer is structured. In words, as patterns.

The line this module keeps: it never downloads the video, never extracts a frame, a voice or a bar of music, never
hands a reference clip to a generator, and never records a real person's likeness or voice for reuse. Profiles name
their source so one can be removed on request. Our films are generated from our own story, our own characters and
our own voices; a profile is a note on craft.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ..providers.google_ai import google_client

REFERENCES_DIR = Path(__file__).resolve().parents[3] / "library" / "references"
DEFAULT_MODEL = "gemini-2.5-pro"
SAMPLE_FPS = 0.25               # four frames a second is far more than craft notes need; one every 4 s keeps cost low


class ScoreNotes(BaseModel):
    cue_anatomy: str = Field(description="how cues start, build, hit and leave; where silence sits")
    instruments_by_rasa: dict[str, str] = Field(description="rasa -> instruments and textures that carried it")
    motifs: str = Field(description="recurring themes, who they belong to, when they return")
    tempo_and_percussion: str


class EmotionNotes(BaseModel):
    beat_pacing: str = Field(description="how long emotional beats are held, how they are entered and left")
    rasa_curve: list[str] = Field(description="the dominant rasa of each act or major movement, in order")
    goosebump_moments: list[str] = Field(description="what made the big moments land: staging, music, cut, line")


class ActionNotes(BaseModel):
    staging: str = Field(description="geography, blocking, how the hero is framed in fights and chases")
    cutting: str = Field(description="shot lengths, coverage, slow motion, impact frames")
    sound: str = Field(description="how hits, whooshes, silence and score share the fight")


class CharacterNotes(BaseModel):
    hero: str
    heroine: str
    villain: str
    supporting: str = Field(description="the comic friend, the elder, the mother, the henchmen: their functions")
    introductions: str = Field(description="how each lead is introduced on screen")


class TrailerNotes(BaseModel):
    structure: str = Field(description="sections, their lengths, where the title lands")
    line_choice: str = Field(description="which kinds of lines a trailer uses and where")
    music_and_cuts: str


class ReferenceProfile(BaseModel):
    title: str
    language: str
    kind: Literal["film", "trailer", "scene", "song", "other"]
    summary: str = Field(description="two sentences on what the piece is and what it does well; no plot spoilers needed")
    score: ScoreNotes
    emotion: EmotionNotes
    action: ActionNotes
    characters: CharacterNotes
    trailer: TrailerNotes
    lessons: list[str] = Field(description="5-10 one-line lessons a new Telugu film could apply")
    cautions: list[str] = Field(description="what not to copy: signature lines, likenesses, specific melodies")


PROMPT = """You are a film analyst for a Telugu production house. Watch the video and write craft notes: how it is
made, not what happens. Describe patterns a new, original film could learn from.

Rules: do not transcribe dialogue or lyrics; do not describe any real person's face or voice in a way that would let
someone reproduce it; do not reproduce melodies in notation; name signature lines or motifs only to list them under
cautions as things not to copy. Answer in English, Telugu terms welcome, as JSON matching the schema.
Focus: {focus}."""


def profile_id(url: str, title: str | None) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")
    vid = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", url)
    return "_".join(x for x in (base[:40], vid.group(1) if vid else "ref") if x)


def learn(url: str, title: str | None = None, what: str = "all", cfg: dict | None = None, client=None,
          out_dir: Path | None = None, start_s: float | None = None, end_s: float | None = None,
          fps: float = SAMPLE_FPS) -> Path:
    """Watch `url` (a public YouTube link) and write library/references/<id>/profile.json (+ manifest.yaml)."""
    from google.genai import types

    g = (cfg or {}).get("google", {})
    client = client or google_client(g, "image")
    meta = {"fps": fps}
    if start_s is not None:
        meta["start_offset"] = f"{int(start_s)}s"
    if end_s is not None:
        meta["end_offset"] = f"{int(end_s)}s"
    video = types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*"),
                       video_metadata=types.VideoMetadata(**meta))
    focus = {"all": "everything in the schema", "score": "the background score above all", "emotion": "emotional beats",
             "action": "action staging and cutting", "characters": "character archetypes and introductions",
             "trailer": "trailer structure"}.get(what, what)
    response = client.models.generate_content(
        model=g.get("analysis_model", DEFAULT_MODEL), contents=[video, PROMPT.format(focus=focus)],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ReferenceProfile,
                                           temperature=0.2))
    profile = ReferenceProfile.model_validate_json(response.text)
    pid = profile_id(url, title or profile.title)
    folder = Path(out_dir or REFERENCES_DIR) / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "profile.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    usage = getattr(response, "usage_metadata", None)
    manifest = {"id": pid, "kind": "reference", "source_url": url, "title": title or profile.title, "learned": what,
                "model": g.get("analysis_model", DEFAULT_MODEL), "sample_fps": fps,
                "watched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "tokens": {"input": getattr(usage, "prompt_token_count", None),
                           "output": getattr(usage, "candidates_token_count", None)} if usage else None,
                "removal": "delete this folder to forget the profile; nothing else holds the source"}
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return folder / "profile.json"


def load_profiles(folder: Path | None = None, limit: int = 3) -> list[dict]:
    """The newest reference profiles, for the agents' context."""
    folder = Path(folder or REFERENCES_DIR)
    if not folder.exists():
        return []
    items = []
    for p in folder.glob("*/profile.json"):
        try:
            items.append((p.stat().st_mtime, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return [d for _, d in sorted(items, key=lambda t: -t[0])[:limit]]


def examples_text(profiles: list[dict], parts: tuple[str, ...] = ("summary", "lessons")) -> str:
    """A compact block for an agent's context: title, summary and lessons (plus the parts asked for)."""
    if not profiles:
        return ""
    out = []
    for d in profiles:
        block = {"title": d.get("title"), **{k: d.get(k) for k in parts if k in d}}
        out.append(json.dumps(block, ensure_ascii=False, indent=1))
    return "\n".join(out)
