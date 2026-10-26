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

REFERENCES_DIR = Path(__file__).resolve().parents[3] / "custom_model_feed" / "notes"   # the custom model feed
DEFAULT_MODEL = "gemini-3.1-pro-preview"      # gemini-2.5-pro is no longer served to the studio's project (404)
DEFAULT_LOCATION = "global"                   # where Vertex AI serves it (not us-central1)
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


# the kinds of Telugu film a reference is an example of; a film is matched to references by these (film_categories)
CATEGORIES = ("action", "mass", "family", "drama", "romance", "comedy", "thriller", "horror", "devotional",
              "mythological", "village", "kids", "animal", "fantasy", "historical", "social", "musical", "sports")
Category = Literal["action", "mass", "family", "drama", "romance", "comedy", "thriller", "horror", "devotional",
                   "mythological", "village", "kids", "animal", "fantasy", "historical", "social", "musical", "sports"]
# whole words in a film's story bible (genres, tone, structure, logline) that make it one of the categories; a
# word ending in * is a stem (mytholog* matches mythological, mythology)
CATEGORY_WORDS = {
    "action": ("action", "fight", "fights", "fighting", "chase", "battle", "battles", "war", "wars"),
    "mass": ("mass", "commercial", "masala", "elevation"),
    "family": ("family", "families", "mother", "father", "brother", "sister", "family_drama"),
    "drama": ("drama", "emotional", "family_drama"), "romance": ("romance", "romantic", "love", "lovers"),
    "comedy": ("comedy", "comic", "funny", "humour", "humor"),
    "thriller": ("thriller", "suspense", "mystery", "crime", "detective"), "horror": ("horror", "ghost", "haunted"),
    "devotional": ("devotional", "bhakti", "temple", "god", "goddess", "divine"),
    "mythological": ("mytholog*", "myth", "myths", "epic", "puran*", "avatar"),
    "village": ("village", "villages", "rural", "palle", "village_action"),
    "kids": ("kids", "children", "child", "childrens"),
    "animal": ("animal", "animals", "monkey", "monkeys", "elephant", "tiger", "dog", "dogs", "bird", "birds"),
    "fantasy": ("fantasy", "magic", "magical", "supernatural"), "historical": ("historical", "history"),
    "social": ("social", "society", "politic*"), "musical": ("musical", "music", "song", "songs", "dance"),
    "sports": ("sport*", "cricket*", "kabaddi")}


def _says(words: set[str], word: str) -> bool:
    return any(w.startswith(word[:-1]) for w in words) if word.endswith("*") else word in words


def film_categories(bible) -> list[str]:
    """The categories a film is, read from its story bible (genres, tone, structure, logline; a plot alone before
    there is one), word by word: which references its agents learn from first."""
    text = " ".join([*(getattr(bible, "genres", None) or []), str(getattr(bible, "tone", "") or ""),
                     str(getattr(bible, "structure_template", "") or ""), str(getattr(bible, "logline", "") or "")])
    words = set(re.findall(r"[a-z_]+", text.lower()))
    return [c for c in CATEGORIES if any(_says(words, w) for w in CATEGORY_WORDS[c])]


class ReferenceProfile(BaseModel):
    title: str
    language: str
    kind: Literal["film", "trailer", "scene", "song", "other"]
    categories: list[Category] = Field(description="the kinds of Telugu film this piece is an example of, from the "
                                                   "list; all that apply")
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


class LearnCostError(RuntimeError):
    """A watch that costs more than the runner spends without asking; `minutes` and `usd` say what it would be,
    for the control room's approve button."""

    def __init__(self, message: str, minutes: float = 0.0, usd: float = 0.0):
        super().__init__(message)
        self.minutes, self.usd = minutes, usd


DEFAULT_MINUTES = 150.0          # a feature film, when nothing says how long the video is


def video_minutes(url: str, fetch=None) -> float | None:
    """The video's length, read from its public YouTube page (the page only: no key, nothing downloaded); None when
    it cannot be read. `fetch(page_url) -> html` for tests."""
    vid = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", url)
    if not vid:
        return None
    page_url = f"https://www.youtube.com/watch?v={vid.group(1)}"
    try:
        if fetch is None:
            import urllib.request
            req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"})
            with urllib.request.urlopen(req, timeout=10) as r:
                fetch_text = r.read(4_000_000).decode("utf-8", "replace")
        else:
            fetch_text = fetch(page_url)
    except Exception:
        return None
    m = re.search(r'"lengthSeconds":"(\d+)"', fetch_text or "")
    return round(int(m.group(1)) / 60, 1) if m and int(m.group(1)) > 0 else None


def estimate_usd(minutes: float, fps: float = SAMPLE_FPS, pricing: dict | None = None) -> tuple[float, int]:
    """(dollars, input tokens) for watching `minutes` of video at `fps` sampled frames a second, with the model's
    audio track, from pricing.gemini_video (planning numbers)."""
    g = (pricing or {}).get("gemini_video") or {}
    seconds = max(0.0, minutes) * 60
    tokens = int(seconds * (fps * g.get("tokens_per_frame", 258) + g.get("audio_tokens_per_s", 32))) + 1500
    rate = g.get("input_per_1m_long", 2.50) if tokens > 200_000 else g.get("input_per_1m", 1.25)
    usd = tokens / 1e6 * rate + 4000 / 1e6 * g.get("output_per_1m", 10.0)
    return round(usd, 3), tokens


def profile_id(url: str, title: str | None) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")
    vid = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", url)
    return "_".join(x for x in (base[:40], vid.group(1) if vid else "ref") if x)


def learn(url: str, title: str | None = None, what: str = "all", cfg: dict | None = None, client=None,
          out_dir: Path | None = None, start_s: float | None = None, end_s: float | None = None,
          fps: float = SAMPLE_FPS, minutes: float | None = None, max_usd: float = 1.0, yes: bool = False,
          pricing: dict | None = None) -> Path:
    """Watch `url` (a public YouTube link) and write custom_model_feed/notes/<id>/profile.json (+ manifest.yaml).
    The cost is estimated first from the length (`minutes`, else end-start, else a feature's 150 min) and the call
    is refused above `max_usd` unless `yes`."""
    from google.genai import types

    guessed = False
    if minutes is None:
        if start_s is not None and end_s is not None:
            minutes = (end_s - start_s) / 60
        else:
            minutes = video_minutes(url)                  # the real length, from the video's page
            guessed = minutes is None
            if guessed:
                print(f"  {url}: its length could not be read from YouTube; priced as a {DEFAULT_MINUTES:.0f} min "
                      "film (give the length on the References page for a true price)", flush=True)
            minutes = minutes or DEFAULT_MINUTES
    est, tokens = estimate_usd(minutes, fps, pricing)
    if est > max_usd and not yes:
        what = (f"this video (length not known, priced as {minutes:.0f} min)" if guessed
                else f"this {minutes:.0f} min video")
        raise LearnCostError(f"watching {what} costs about ${est:.2f} ({tokens:,} tokens), over the ${max_usd:.2f} "
                             "limit: approve it on the References page (or run nraiorbit learn with --max-usd, "
                             "--minutes or --start/--end)", minutes=minutes, usd=est)
    g = (cfg or {}).get("google", {})
    client = client or google_client({**g, "analysis_location": g.get("analysis_location") or DEFAULT_LOCATION},
                                     "analysis")          # the watching model's own region
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
        model=g.get("analysis_model") or DEFAULT_MODEL, contents=[video, PROMPT.format(focus=focus)],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ReferenceProfile,
                                           temperature=0.2))
    profile = ReferenceProfile.model_validate_json(response.text)
    pid = profile_id(url, title or profile.title)
    folder = Path(out_dir or REFERENCES_DIR) / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "profile.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    usage = getattr(response, "usage_metadata", None)
    manifest = {"id": pid, "kind": "reference", "source_url": url, "title": title or profile.title, "learned": what,
                "model": g.get("analysis_model") or DEFAULT_MODEL, "sample_fps": fps,
                "watched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "tokens": {"input": getattr(usage, "prompt_token_count", None),
                           "output": getattr(usage, "candidates_token_count", None)} if usage else None,
                "removal": "delete this folder to forget the profile; nothing else holds the source"}
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    gv = (pricing or {}).get("gemini_video") or {}
    used_in = getattr(usage, "prompt_token_count", None) or tokens
    used_out = getattr(usage, "candidates_token_count", None) or 4000
    usd = used_in / 1e6 * (gv.get("input_per_1m_long", 2.5) if used_in > 200_000 else gv.get("input_per_1m", 1.25)) \
        + used_out / 1e6 * gv.get("output_per_1m", 10.0)
    with open(folder.parent / "ledger.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"provider": "gemini_video", "item": pid, "usd": round(usd, 4), "tokens_in": used_in,
                            "tokens_out": used_out, "url": url, "watched": manifest["watched"]}) + "\n")
    return folder / "profile.json"


TAGS = "tags.json"           # custom_model_feed/notes/<id>/tags.json: the owner's corrected categories


def profile_categories(folder: Path, profile: dict) -> list[str]:
    """A reference's categories: the owner's correction when there is one, else what Gemini said."""
    try:
        owner = json.loads((Path(folder) / TAGS).read_text(encoding="utf-8"))
        if isinstance(owner, list):
            return [c for c in owner if c in CATEGORIES]
    except (OSError, ValueError):
        pass
    return [c for c in (profile.get("categories") or []) if c in CATEGORIES]


def load_profiles(folder: Path | None = None, limit: int = 3, categories=()) -> list[dict]:
    """The reference profiles for the agents' context: those sharing the most of the film's `categories` first,
    the newest first among equals (with no categories, simply the newest), each with its categories."""
    folder = Path(folder or REFERENCES_DIR)
    if not folder.exists():
        return []
    wanted, items = set(categories or ()), []
    for p in folder.glob("*/profile.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d = {**d, "categories": profile_categories(p.parent, d)}
        items.append((len(wanted & set(d["categories"])), p.stat().st_mtime, d))
    return [d for _, _, d in sorted(items, key=lambda t: (-t[0], -t[1]))[:limit]]


def examples_text(profiles: list[dict], parts: tuple[str, ...] = ("summary", "lessons")) -> str:
    """A compact block for an agent's context: title, summary and lessons (plus the parts asked for)."""
    if not profiles:
        return ""
    out = []
    for d in profiles:
        block = {"title": d.get("title"), "categories": d.get("categories") or [],
                 **{k: d.get(k) for k in parts if k in d}}
        out.append(json.dumps(block, ensure_ascii=False, indent=1))
    return "\n".join(out)
