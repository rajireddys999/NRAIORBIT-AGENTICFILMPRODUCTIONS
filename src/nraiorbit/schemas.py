"""Data contracts between pipeline stages.

LLM-facing models (StoryBible, Outline, SceneScript, ShotList, CueSheet) are passed to
Claude as structured-output schemas, so they stay flat and use Literal enums rather
than validators the API cannot express. Asset manifests are loaded from library YAML.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Rasa = Literal[
    "shringara", "hasya", "karuna", "raudra", "veera", "bhayanaka",
    "bibhatsa", "adbhuta", "shanta", "vatsalya", "bhakti",
]
Dialect = Literal["telangana", "coastal_andhra", "godavari", "rayalaseema", "uttarandhra", "standard"]
CodeMix = Literal["none", "light", "heavy"]
Role = Literal["hero", "heroine", "villain", "supporting", "comic", "mentor", "family", "extra"]
SetPiece = Literal[
    "none", "hero_intro", "fight", "chase", "song", "interval_bang",
    "flashback", "pre_climax", "climax", "emotional_peak", "comedy_track",
]
ShotSize = Literal["EWS", "WS", "MS", "MCU", "CU", "ECU", "OTS", "POV", "two_shot", "insert", "aerial"]
CameraMove = Literal[
    "static", "push_in", "pull_out", "pan", "tilt", "track", "orbit", "crane_up",
    "crane_down", "handheld", "crash_zoom", "dolly_zoom", "whip_pan", "drone",
]
FpsEffect = Literal["normal", "slowmo", "speed_ramp"]


# ---------------------------------------------------------------- Story Bible (S1)

class PhysicalSpec(BaseModel):
    gender: str
    age: int
    height_cm: int
    build: str = Field(description="e.g. athletic, lean, heavy-set")
    skin_tone: str
    face: str = Field(description="Distinctive facial features, for the identity pack brief")
    hair: str
    marks: str = Field(description="Scars, tattoos, moles; 'none' if none")


class VoiceSpec(BaseModel):
    pitch: Literal["low", "mid", "high"]
    timbre: str = Field(description="e.g. gravelly, warm, nasal, crisp")
    dialect: Dialect
    speech_register: Literal["colloquial", "formal", "rustic", "urban_slang"]
    code_mix: CodeMix
    speech_traits: list[str]


class CharacterSpec(BaseModel):
    id: str = Field(description="lowercase slug, stable for the whole film, e.g. 'arjun'")
    name_te: str
    name_en: str
    role: Role
    physical: PhysicalSpec
    costume_baseline: str
    personality: str
    arc: str
    voice: VoiceSpec


class Beat(BaseModel):
    id: str
    summary: str
    rasa: Rasa
    intensity: int = Field(description="1 (quiet) to 10 (peak)")
    characters: list[str]
    set_piece: SetPiece


class Act(BaseModel):
    name: str
    purpose: str
    target_minutes: float
    beats: list[Beat]


class StoryBible(BaseModel):
    title_te: str
    title_en: str
    logline: str
    genres: list[str]
    tone: str
    structure_template: Literal["telugu_commercial", "family_drama", "thriller", "village_action"]
    era_and_region: str
    world: str
    themes: list[str]
    characters: list[CharacterSpec]
    acts: list[Act]


# ---------------------------------------------------------------- Outline (S2)

class SceneOutline(BaseModel):
    scene_id: str = Field(description="sc001, sc002, ... in film order")
    beat_ids: list[str]
    heading: str = Field(description="INT./EXT. LOCATION - TIME")
    location_hint: str
    time_of_day: Literal["dawn", "morning", "noon", "evening", "golden_hour", "dusk", "night"]
    characters: list[str]
    synopsis: str
    rasa_primary: Rasa
    rasa_secondary: Rasa | None
    intensity: int
    set_piece: SetPiece
    target_seconds: int


class Outline(BaseModel):
    scenes: list[SceneOutline]


# ---------------------------------------------------------------- Scene script (S3)

class Delivery(BaseModel):
    rasa: Rasa
    intensity: int
    pace: Literal["very_slow", "slow", "medium", "fast", "very_fast"]
    volume: Literal["whisper", "low", "normal", "loud", "shout"]
    notes: str


class ScriptBlock(BaseModel):
    """One unit of the scene: an action paragraph or a dialogue line."""

    block_id: str = Field(description="<scene_id>_b001 ...")
    kind: Literal["action", "dialogue"]
    character_id: str | None = Field(description="Speaker for dialogue; null for action")
    text_te: str = Field(description="Dialogue in spoken Telugu, Telugu script; action may be English")
    text_en: str = Field(description="English subtitle (dialogue) or English action description")
    delivery: Delivery | None


class SceneScript(BaseModel):
    scene_id: str
    heading: str
    synopsis: str
    rasa_primary: Rasa
    intensity: int
    blocks: list[ScriptBlock]


# ---------------------------------------------------------------- Shot list (S6)

def _director_schema(schema: dict) -> None:
    """Hide video_tier from Claude (tiers are a spending choice, set by hand) and keep strict output happy."""
    schema.get("properties", {}).pop("video_tier", None)
    schema["required"] = list(schema.get("properties", {}))


class Shot(BaseModel):
    model_config = ConfigDict(json_schema_extra=_director_schema)

    shot_id: str = Field(description="<scene_id>_s001 ...")
    block_ids: list[str] = Field(description="Script blocks this shot covers")
    size: ShotSize
    camera_move: CameraMove
    lens_mm: int
    fps_effect: FpsEffect
    characters_in_frame: list[str]
    costume_ids: list[str] = Field(description="Costume per character in frame, same order")
    action_template_id: str | None = Field(description="library action id if one fits, else null")
    image_prompt: str = Field(description="Keyframe description in English; no character names, use ids")
    motion_prompt: str = Field(description="What moves during the shot")
    duration_s: float
    rasa: Rasa
    intensity: int
    sfx_cues: list[str]
    trailer_candidate: bool
    punch_dialogue: bool = Field(description="Shot carries a quotable/mass dialogue")
    video_tier: str | None = Field(default=None, description="Key of google.video_tiers in pipeline.yaml "
                                   "(standard_1080p, standard_720p, fast_720p, lite_720p); null = the project's "
                                   "default Veo settings")


class ShotList(BaseModel):
    scene_id: str
    shots: list[Shot]


# ---------------------------------------------------------------- Music (S12)

class Cue(BaseModel):
    cue_id: str
    scene_ids: list[str]
    start_s: float
    duration_s: float
    rasa: Rasa
    intensity: int
    cue_type: str
    raga: str
    tala: str
    bpm: int
    instruments: list[str]
    leitmotif_character: str | None
    peak: bool
    silence_before_s: float = 0.0
    notes: str = ""          # free-text direction for the music generator, e.g. a trailer's timed elevation arc


class CueSheet(BaseModel):
    cues: list[Cue]


class CueRevision(BaseModel):
    """Music Director edits to the deterministic plan. Timing fields are not editable."""

    cue_id: str
    raga: str | None
    tala: str | None
    bpm: int | None
    instruments: list[str] | None
    leitmotif_character: str | None
    silence_before_s: float | None
    reason: str


class CueRevisions(BaseModel):
    revisions: list[CueRevision]


# ---------------------------------------------------------------- Casting (S4)

class IdentityPackBrief(BaseModel):
    """Brief for building a new character identity pack (docs/CHARACTER_IDENTITY.md §1)."""

    character_id: str
    visual_brief: str
    face_prompts: list[str] = Field(description="Angle and expression prompts for the face refs")
    body_prompts: list[str]
    costume_prompts: list[str]
    negative_prompt: str
    library_tags: list[str]


# ---------------------------------------------------------------- Critique

class Critique(BaseModel):
    score: int = Field(description="0-10 against the rubric")
    issues: list[str]
    revision_instructions: list[str]


# ---------------------------------------------------------------- Asset manifests

class License(BaseModel):
    type: Literal["owned", "commissioned", "licensed", "cc0", "cc_by", "unknown"] = "unknown"
    commercial_use: bool = False
    source: str | None = None
    document_ref: str | None = None


class Consent(BaseModel):
    required: bool = False
    document_ref: str | None = None


class AssetBase(BaseModel):
    id: str
    kind: str
    version: int = 1
    name: str | None = None
    description: str | None = None
    tags: list[str] = []
    rasas: list[Rasa] = []
    intensity: tuple[int, int] | None = None
    license: License = License()
    consent: Consent = Consent()
    files: dict[str, str] = {}

    @property
    def ref(self) -> str:
        return f"{self.kind}s/{self.id}@v{self.version}"

    def usable_for_production(self) -> tuple[bool, list[str]]:
        problems = []
        if not self.license.commercial_use:
            problems.append("license.commercial_use is false")
        if self.consent.required and not self.consent.document_ref:
            problems.append("consent required but no document_ref")
        return (not problems, problems)


class Costume(BaseModel):
    id: str
    description: str
    refs: str | None = None


class CharacterAsset(AssetBase):
    kind: Literal["character"] = "character"
    role: Role | None = None
    physical: dict = {}
    costumes: list[Costume] = []
    guard: dict[str, float] = {}
    default_voice: str | None = None


class VoiceAsset(AssetBase):
    kind: Literal["voice"] = "voice"
    dialect: Dialect = "standard"
    gender: str | None = None
    age_range: tuple[int, int] | None = None
    pitch: Literal["low", "mid", "high"] | None = None
    timbre_tags: list[str] = []
    tts_speaker: str | None = None      # named speaker of the TTS model, for a consistent voice
    gemini_voice: str | None = None     # Gemini TTS prebuilt voice name
    emotion_refs: dict[str, list[dict]] = {}


class MusicStemAsset(AssetBase):
    kind: Literal["music_stem"] = "music_stem"
    role: Literal["groove", "melody", "texture", "hit", "riser", "chant", "drone"]
    raga: str | None = None
    tala: str | None = None
    bpm: int | None = None
    sa_note: str | None = None
    instruments: list[str] = []
    duration_s: float | None = None
    loopable: bool = False


class MotifAsset(AssetBase):
    kind: Literal["motif"] = "motif"
    for_role: Role | None = None
    raga: str
    sa_note: str | None = None
    bpm: int | None = None


class ActionAsset(AssetBase):
    kind: Literal["action"] = "action"
    category: Literal["fight", "chase", "dance", "stunt", "walk", "everyday", "entry"]
    performers: int = 1
    duration_s: float | None = None
    camera_suggestions: list[str] = []
    sfx_cues: list[str] = []


class LocationAsset(AssetBase):
    kind: Literal["location"] = "location"
    region: str | None = None
    setting_type: str
    times_of_day: list[str] = []


class SfxAsset(AssetBase):
    kind: Literal["sfx"] = "sfx"
    category: str
    duration_s: float | None = None


ASSET_TYPES: dict[str, type[AssetBase]] = {
    "character": CharacterAsset,
    "voice": VoiceAsset,
    "music_stem": MusicStemAsset,
    "motif": MotifAsset,
    "action": ActionAsset,
    "location": LocationAsset,
    "sfx": SfxAsset,
}


# ---------------------------------------------------------------- Cast lockfile

class CastEntry(BaseModel):
    character_asset: str
    voice_asset: str | None = None
    leitmotif_asset: str | None = None
    costumes: dict[str, str] = {}


class CastLock(BaseModel):
    lock_version: int = 1
    characters: dict[str, CastEntry]
