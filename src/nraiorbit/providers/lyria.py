"""Google Lyria 3 background score (google-genai SDK, Vertex AI or the Gemini API): one generation per cue.

    providers.music: lyria
    google.music_model: lyria-3-pro-preview     # longer tracks; lyria-3-clip-preview makes 30 s clips (looped)
    google.music_location: global               # Vertex AI serves Lyria 3 from global only

The prompt is written from the cue sheet in the vocabulary of docs/MUSIC_TELUGU.md (rasa, raga, tala, tempo,
instruments, leitmotif, the mass-elevation recipe for peaks and the trailer), plus Cue.notes (e.g. the trailer's
timed elevation arc). The track is trimmed with a fade-out, or crossfade-looped, to the cue's exact length.
A file in inbox/music/<cue_id>.* wins over generation. Raw generations are cached in cache/music/ by model and
prompt, so a re-render never pays twice; every paid call is checked against the budget and logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import wave
from pathlib import Path

from ..costing import SpendLedger, lyria_price
from ..post import ffmpeg
from ..schemas import Cue
from .base import register
from .google_ai import GoogleAPIError, _call, _types, google_client
from .inbox import InboxMusic, _find_audio

DEFAULT_MODEL = "lyria-3-pro-preview"
CLIP_SECONDS = 30                 # Lyria 3 Clip's fixed length
FADE_OUT_S = 1.5
EXT = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/vnd.wave": ".wav", "audio/mpeg": ".mp3",
       "audio/mp3": ".mp3", "audio/ogg": ".ogg", "audio/ogg_opus": ".ogg", "audio/opus": ".opus", "audio/flac": ".flac",
       "audio/x-flac": ".flac", "audio/aac": ".aac", "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a",
       "audio/webm": ".webm", "audio/aiff": ".aiff"}
NOTE = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
SHARP = {1: "C#", 3: "D#", 8: "G#", 10: "A#"}

RASA_MOOD = {
    "shringara": "romantic, tender and beautiful", "hasya": "light, comic and playful",
    "karuna": "sorrowful pathos, deeply emotional", "raudra": "furious, aggressive and menacing",
    "veera": "heroic, triumphant mass elevation", "bhayanaka": "dread, fear and suspense",
    "bibhatsa": "uneasy, repulsive and grotesque", "adbhuta": "wonder, awe and revelation",
    "shanta": "peaceful, calm and resolved", "vatsalya": "warm family affection, tender",
    "bhakti": "devotional, sacred and festive",
}
RAGA_COLOUR = {   # how each raga sounds, for a model that may not know Carnatic names
    "mohanam": "Mohanam (major pentatonic: bright, romantic, auspicious)",
    "kalyani": "Kalyani (Lydian colour with a raised fourth: majestic, luminous)",
    "abheri": "Abheri (Dorian-like minor: tender longing)",
    "hindolam": "Hindolam (minor pentatonic: meditative, serene)",
    "bilahari": "Bilahari (major: bright, playful)",
    "shivaranjani": "Shivaranjani (minor pentatonic: aching, sorrowful)",
    "subhapantuvarali": "Subhapantuvarali (minor with a flat second and raised fourth: deep grief)",
    "mukhari": "Mukhari (minor: plaintive lament)",
    "charukesi": "Charukesi (major with a flat sixth and seventh: bittersweet)",
    "atana": "Atana (major with a flat seventh: vigorous, fiery)",
    "nattai": "Nattai (bold, martial raga with an augmented second: heroic, auspicious)",
    "arabhi": "Arabhi (major: energetic, triumphant)",
    "hamsadhwani": "Hamsadhwani (major pentatonic without fourth and sixth: festive, triumphant)",
    "vasantha": "Vasantha (flat second, major third: majestic wonder)",
    "revati": "Revati (pentatonic with a flat second: sombre, devotional calm)",
    "neelambari": "Neelambari (major: gentle lullaby)",
    "ananda_bhairavi": "Ananda Bhairavi (Dorian-like: warm, tender)",
    "mayamalavagowla": "Mayamalavagowla (double harmonic scale, flat second and sixth: devotional, dramatic minor colour)",
    "bhairavi": "Bhairavi (minor: grave, devotional)",
}
NO_RAGA = {"textural": "No raga melody: non-tonal textures, drones and swells.",
           "chromatic_hybrid": "Chromatic, dissonant hybrid harmony over a Carnatic pulse."}
TALA_FEEL = {"adi": "Adi tala (8-beat cycle)", "rupaka": "Rupaka tala (3-beat cycle, lilting 6/8 feel)",
             "misra_chapu": "Misra Chapu (7-beat cycle)", "khanda_chapu": "Khanda Chapu (5-beat cycle)",
             "teenmaar_folk": "teenmaar folk groove (dappu pattern)", "folk": "teenmaar folk groove (dappu pattern)"}
INSTRUMENT_PHRASE = {
    "thavil": "thunderous thavil", "dappu": "loud dappu folk frame drums", "chenda": "pounding chenda drums",
    "nadaswaram": "piercing nadaswaram", "nadaswaram_slow": "slow, mournful nadaswaram", "mridangam": "mridangam",
    "soft_mridangam": "soft mridangam patterns", "veena": "Saraswati veena plucks and glides",
    "veena_glissando": "veena glissandi", "venu_flute": "bansuri (venu) bamboo flute", "bansuri": "bansuri bamboo flute",
    "flute": "bansuri bamboo flute", "violin": "violin section", "violin_section": "violin section",
    "carnatic_violin_solo": "expressive Carnatic violin solo with gamakas", "strings": "lush string section",
    "soft_strings": "soft strings", "strings_swell": "string swells", "pizzicato_strings": "pizzicato strings",
    "dissonant_strings": "dissonant string clusters", "detuned_strings": "detuned strings", "cello": "deep cello",
    "epic_brass": "epic brass stabs and swells", "low_brass": "low brass", "santoor": "shimmering santoor",
    "mandolin": "playful Carnatic mandolin", "ghatam": "ghatam clay-pot rhythm", "kanjira": "kanjira frame drum",
    "morsing": "twangy morsing (jaw harp)", "clarinet": "cheeky clarinet", "whistles": "playful whistles",
    "whistle_claps": "crowd whistles and claps", "shehnai": "plaintive shehnai", "tanpura_drone": "tanpura drone",
    "taiko_style_drums": "huge taiko-style drums", "distorted_guitar": "distorted electric guitar",
    "electric_guitar": "electric guitar riffs", "low_drones": "low drones", "prepared_percussion": "prepared percussion",
    "reverse_swells": "reverse swells", "breath_fx": "breathy textures", "metallic_scrapes": "metallic scrapes",
    "sub_bass": "sub bass", "harp": "harp", "celesta": "celesta", "temple_bells": "temple bells",
    "conch": "conch shell call", "synth_pads": "warm synth pads", "synth_pad": "warm synth pads",
    "choir": "wordless choir", "choir_sanskrit_chant": "big wordless choir chant (Sanskrit-style vowels)",
    "male_chant": "wordless male chant", "group_chant": "wordless group chant", "female_alaap": "wordless female alaap",
    "female_hum": "wordless female hum", "female_lullaby_hum": "wordless female lullaby hum",
}
VOCAL_TEXTURES = {"choir", "choir_sanskrit_chant", "male_chant", "group_chant", "female_alaap", "female_hum",
                  "female_lullaby_hum"}
ROLE_WORDS = {"hero": "the hero", "heroine": "the heroine", "villain": "the villain"}
MASS = ("Mass elevation, Telugu hero style: thunderous thavil and dappu, epic brass stabs, a rising build, "
        "big hits on the accents, wordless choir chant, crowd whistles; huge, theatrical, larger than life.")
TRAILER_ARC = ("Arc: low drone and heartbeat hits, a rising thavil and dappu roll, a beat of silence, then a huge "
               "full-band hit and a driving finale that keeps climbing.")


def music_model(google_cfg: dict) -> str:
    return google_cfg.get("music_model") or DEFAULT_MODEL


def cue_seconds(cue: Cue) -> float:
    """The audible part of a cue (after its silence), as every music provider renders it."""
    return max(0.5, cue.duration_s - cue.silence_before_s)


def characters(project_root: Path) -> dict[str, tuple[str, str]]:
    """character id -> (English name, role) from the Story Bible, to name leitmotifs."""
    path = project_root / "01_story_bible.json"
    if not path.exists():
        return {}
    return {c["id"]: (c.get("name_en") or c["id"], c.get("role") or "")
            for c in json.loads(path.read_text(encoding="utf-8")).get("characters", [])}


def _words(s: str) -> str:
    return s.replace("_", " ")


def scale_notes(scale: list[int]) -> str:
    """Pitch classes on C, spelled so a step like Nattai's augmented second reads D# E, not Eb E."""
    pcs = {p % 12 for p in scale}
    return " ".join(SHARP[p] if p in SHARP and (p + 1) % 12 in pcs and p - 1 not in pcs else NOTE[p] for p in sorted(pcs))


def cue_prompt(cue: Cue, model: str = DEFAULT_MODEL, chars: dict[str, tuple[str, str]] | None = None,
               navarasa: dict | None = None) -> str:
    seconds = cue_seconds(cue)
    big = cue.peak or cue.cue_type == "trailer"
    level = ("gentle and restrained" if cue.intensity <= 3 else "moderate" if cue.intensity <= 6
             else "strong and driving" if cue.intensity <= 8 else "maximum intensity, huge")
    parts = ["Instrumental Telugu film background score (BGM). No vocals, no lyrics, no spoken words.",
             f"Mood: {RASA_MOOD.get(cue.rasa, _words(cue.rasa))} ({cue.rasa} rasa), intensity {cue.intensity}/10, "
             f"{level}. Cue: {_words(cue.cue_type)}."]
    if cue.raga in NO_RAGA:
        parts.append(NO_RAGA[cue.raga])
    else:
        scale = (navarasa or {}).get("raga_scales", {}).get(cue.raga)
        notes = f", notes from tonic Sa=C: {scale_notes(scale)}" if scale else ""
        parts.append(f"Melody in Carnatic raga {RAGA_COLOUR.get(cue.raga, _words(cue.raga).title())}{notes}.")
    parts.append("Free time, no steady pulse." if cue.tala == "free"
                 else f"Rhythm: {TALA_FEEL.get(cue.tala, _words(cue.tala) + ' tala')}, about {cue.bpm} BPM.")
    if cue.instruments:
        parts.append(f"Instruments: {', '.join(INSTRUMENT_PHRASE.get(i, _words(i)) for i in cue.instruments)}.")
    if cue.leitmotif_character:
        name, role = (chars or {}).get(cue.leitmotif_character, (cue.leitmotif_character.title(), ""))
        who = f"{name}, {ROLE_WORDS[role]}" if role in ROLE_WORDS else name
        parts.append(f"Weave in a short, memorable leitmotif for {who}, stated clearly in the raga.")
    if big:
        parts.append(MASS)
        if cue.cue_type == "trailer" and not cue.notes.strip():
            parts.append(TRAILER_ARC)
        elif cue.cue_type != "trailer":
            parts.append("Start on a huge full-band hit, then drive a mass groove with more hits on the accents.")
    else:
        parts.append("Leave space for dialogue: not too dense in the mid range (voice frequencies), "
                     "no busy lead melody all the way through.")
    if big or VOCAL_TEXTURES & set(cue.instruments):
        parts.append("The only voices allowed are wordless textures (chant, alaap, hum: pure vowels, never words).")
    target = min(seconds, CLIP_SECONDS) if "clip" in model else seconds
    parts.append(f"Length: about {round(target)} seconds"
                 + (", seamlessly loopable: steady groove, no intro or ending." if target < seconds - 0.5 else "."))
    if cue.notes.strip():
        notes = cue.notes.strip()
        parts.append(f"Structure (follow these timings): {notes}{'' if notes[-1] in '.!?' else '.'}")
    parts.append("Cinematic, high-fidelity stereo mix.")
    return " ".join(parts)


def cache_dir(project_root: Path) -> Path:
    return project_root / "cache" / "music"


def cache_key(model: str, prompt: str) -> str:
    return hashlib.sha1((model + prompt).encode("utf-8")).hexdigest()[:16]


def cached_raw(project_root: Path, model: str, prompt: str) -> Path | None:
    """The raw generation for this model + prompt, if it was already paid for."""
    return next(iter(sorted(cache_dir(project_root).glob(f"{cache_key(model, prompt)}.*"))), None)


def first_audio(response) -> tuple[bytes, str]:
    """The first audio/* inline part (text parts are ignored); no audio -> GoogleAPIError with the reason."""
    texts = []
    for cand in getattr(response, "candidates", None) or []:
        for part in (cand.content.parts if getattr(cand, "content", None) else None) or []:
            blob = getattr(part, "inline_data", None)
            if blob is not None and blob.data and (blob.mime_type or "").lower().startswith("audio/"):
                data = base64.b64decode(blob.data) if isinstance(blob.data, str) else blob.data
                return data, blob.mime_type
            if getattr(part, "text", None):
                texts.append(part.text)
    cands = getattr(response, "candidates", None) or []
    reason = getattr(cands[0], "finish_reason", None) if cands else None
    feedback = getattr(response, "prompt_feedback", None)
    text = " ".join(texts)[:400]
    raise GoogleAPIError(
        f"no audio in the response (finish_reason={reason}, prompt_feedback={feedback}, text={text!r}). If the "
        "model answered in text only, try google.music_response_modalities: [AUDIO, TEXT] (or null); if it was "
        "blocked, change the cue (or put a track in inbox/music/). Re-run to resume.")


def _store(data: bytes, mime: str, folder: Path, key: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    kind, *params = [p.strip() for p in mime.split(";")]
    kind = kind.lower()
    tmp = folder / f".{key}.part"                     # renamed when complete: a crash never leaves a bad cache
    if kind in ("audio/l16", "audio/pcm"):            # raw 16-bit PCM (as Gemini TTS sends it): add a WAV header
        opts = dict(p.split("=", 1) for p in params if "=" in p)
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(int(opts.get("channels", 1)))
            w.setsampwidth(2)
            w.setframerate(int(opts.get("rate", ffmpeg.SAMPLE_RATE)))
            w.writeframes(data)
        ext = ".wav"
    else:
        tmp.write_bytes(data)
        ext = EXT.get(kind) or "." + (re.sub(r"[^a-z0-9]", "", kind.split("/")[-1]) or "audio")
    dest = folder / f"{key}{ext}"
    os.replace(tmp, dest)
    return dest


def fit_to_cue(src: Path, seconds: float, out: Path) -> Path:
    """Exactly `seconds` of 48 kHz stereo: a longer track is trimmed with a 1.5 s fade-out, a shorter one is
    looped with crossfades until long enough, then trimmed and faded."""
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        have = ffmpeg.duration(src)
    except (subprocess.CalledProcessError, KeyError, ValueError):   # an audio format ffmpeg cannot read
        raise GoogleAPIError(f"{src}: ffmpeg cannot decode this generation. Delete it to generate again "
                             "(paid again), or put a track in inbox/music/.") from None
    if have < 1.0:
        raise GoogleAPIError(f"{src}: only {have:.2f} s of audio, too short for a cue. Delete it to generate again.")
    norm = f"aresample={ffmpeg.SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"
    end = f"atrim=0:{seconds:.3f},afade=t=out:st={max(0.0, seconds - FADE_OUT_S):.3f}:d={min(FADE_OUT_S, seconds):.3f}"
    if have + 0.05 >= seconds:
        ffmpeg.run(["-i", str(src), "-af", f"{norm},apad,{end}", "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(out)])
        return out
    xf = min(2.0, have / 3)                           # n copies with n-1 crossfades last n*have - (n-1)*xf
    n = max(2, math.ceil((seconds - xf) / (have - xf)))
    graph = [f"[{i}:a]{norm}[s{i}]" for i in range(n)]
    prev = "s0"
    for i in range(1, n):
        graph.append(f"[{prev}][s{i}]acrossfade=d={xf:.3f}:c1=tri:c2=tri[x{i}]")
        prev = f"x{i}"
    graph.append(f"[{prev}]{end}[out]")
    ffmpeg.run([*(a for _ in range(n) for a in ("-i", str(src))), "-filter_complex", ";".join(graph),
                "-map", "[out]", "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(out)])
    return out


@register("music", "lyria")
class LyriaMusic:
    def __init__(self, project_root: Path, cfg: dict, pricing: dict, ledger: SpendLedger, navarasa: dict | None = None,
                 client=None):
        self.root = project_root
        self.g = cfg.get("google", {})
        self.model = music_model(self.g)
        self.price = lyria_price(pricing, self.model)
        self.ledger = ledger
        self.navarasa = navarasa or {}
        self.inbox = InboxMusic(project_root)
        self.chars = characters(project_root)
        self._client = client                         # made on the first paid call: cached cues need no sign-in

    @property
    def client(self):
        if self._client is None:
            self._client = google_client({**self.g, "music_location": self.g.get("music_location") or "global"}, "music")
        return self._client

    def prompt(self, cue: Cue) -> str:
        return cue_prompt(cue, self.model, self.chars, self.navarasa)

    def render_cue(self, cue: Cue, out: Path) -> Path:
        if _find_audio(self.inbox.folder, cue.cue_id):  # a composer's or hand-made track wins, no call
            return self.inbox.render_cue(cue, out)
        prompt = self.prompt(cue)
        raw = cached_raw(self.root, self.model, prompt) or self._generate(cue, prompt)
        return fit_to_cue(raw, cue_seconds(cue), out)

    def _generate(self, cue: Cue, prompt: str) -> Path:
        what = f"Lyria {cue.cue_id}"
        self.ledger.check(self.price, what)
        modalities = self.g.get("music_response_modalities", ["AUDIO"])
        config = _types().GenerateContentConfig(response_modalities=list(modalities)) if modalities else None
        response = _call(self.client.models.generate_content, what, model=self.model, contents=prompt, config=config)
        try:
            data, mime = first_audio(response)
        except GoogleAPIError as e:
            self.ledger.record("lyria", cue.cue_id, 0.0, model=self.model, note="no audio returned: assumed not billed")
            raise GoogleAPIError(f"{what}: {e}") from None
        self.ledger.record("lyria", cue.cue_id, self.price, model=self.model, mime=mime)
        return _store(data, mime, cache_dir(self.root), cache_key(self.model, prompt))
