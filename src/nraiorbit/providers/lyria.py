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

import numpy as np

from ..costing import SpendLedger, lyria_price
from ..post import ffmpeg
from ..planning.score import load_styles, slot_for
from ..schemas import Cue
from .base import register
from .google_ai import GoogleAPIError, _call, _types, google_client
from .inbox import InboxMusic, _find_audio

DEFAULT_MODEL = "lyria-3-pro-preview"
CLIP_SECONDS = 30                 # Lyria 3 Clip's fixed length
FADE_OUT_S = 1.5
SHORT_PAD_S = 4.0                 # a track up to this much (or 12%) short is padded at the end, never looped
EXTRA_S = 2                       # ask for a little more than the cue needs; the edit trims (timing drifts)
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
MASS_VILLAIN = ("Villain menace, Telugu mass style: crushing low brass and taiko, pounding thavil and chenda, distorted "
                "low guitars, dark wordless male chant, heavy hits; dangerous, towering, merciless.")
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
               navarasa: dict | None = None, style: dict | None = None) -> str:
    """style: the cue's slot preset from config/music_styles.yaml (planning/score.py); its style line replaces the
    generic mass / dialogue-space wording, and its timed sections arrive in cue.notes."""
    seconds = cue_seconds(cue)
    big = cue.peak or cue.cue_type == "trailer"
    level = ("gentle and restrained" if cue.intensity <= 3 else "moderate" if cue.intensity <= 6
             else "strong and driving" if cue.intensity <= 8 else "maximum intensity, huge")
    voices = big or bool(VOCAL_TEXTURES & set(cue.instruments)) or bool((style or {}).get("wordless_vocals"))
    opening = ("Telugu film background score (BGM). Wordless vocals only: no lyrics, no sung or spoken words."
               if voices else "Instrumental Telugu film background score (BGM). Instrumental only, no vocals, no lyrics, "
                              "no spoken words.")   # "instrumental" removes every voice, so not when a choir is wanted
    parts = [opening,
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
    if style and style.get("style"):
        parts.append(f"Style: {' '.join(style['style'].split())}")
        if cue.cue_type == "trailer" and not cue.notes.strip():
            parts.append(TRAILER_ARC)
    elif big:
        parts.append(MASS_VILLAIN if cue.rasa == "raudra" and cue.cue_type != "trailer" else MASS)
        if cue.cue_type == "trailer" and not cue.notes.strip():
            parts.append(TRAILER_ARC)
        elif cue.cue_type != "trailer":
            parts.append("Start on a huge full-band hit, then drive a mass groove with more hits on the accents.")
    else:
        parts.append("Leave space for dialogue: not too dense in the mid range (voice frequencies), "
                     "no busy lead melody all the way through.")
    if voices:
        parts.append("The only voices allowed are wordless textures (chant, alaap, hum: pure vowels, never words).")
    loop = "clip" in model and seconds > CLIP_SECONDS + 0.5
    target = CLIP_SECONDS if "clip" in model else math.ceil(seconds) + EXTRA_S   # aim long and trim: a short track
    parts.append(f"Length: about {target} seconds"                                # would loop its opening back in
                 + (", seamlessly loopable: steady groove, no intro or ending." if loop else "."))
    if cue.notes.strip():
        notes = cue.notes.strip()
        parts.append(f"Structure (follow these timings): {notes}{'' if notes[-1] in '.!?' else '.'}")
    emotional = cue.rasa in ("karuna", "vatsalya") or (style or {}).get("shape") == "emotion"
    if big:
        parts.append("Mix: strong dynamic contrast, quiet builds well below the hits, one decisive silence before the "
                     "main impact, impacts layered from transient, body and sub-bass; cinematic, high-fidelity stereo.")
        parts.append("Avoid: lyrics, pop vocals, four-on-the-floor EDM kick, cheerful or comedic tone, a constant wall "
                     "of sound.")
    elif emotional:
        parts.append("Mix: wide dynamic range from very soft to very loud; at the peak push the upper strings, the high "
                     "voice and the reed forward; long hall reverb; cinematic, high-fidelity stereo.")
        parts.append("Avoid: lyrics, drums in the opening, cheerful tone, heavy compression before the climax.")
    else:
        parts.append("Cinematic, high-fidelity stereo mix.")
        parts.append("Avoid: lyrics, sung words, a busy lead over dialogue.")
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
        "model answered in text only, check google.music_response_modalities (Lyria 3 needs [AUDIO, TEXT]); if it was "
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


HIT_MARK = re.compile(r"\[(\d+):(\d\d) - \d+:\d\d\] (Hit|Peak):")
SEARCH_S = {"Hit": (-4.0, 12.0), "Peak": (-8.0, 8.0)}   # where to look for the generated moment, around the plan
ENV_HOP_S = 0.05
SILENCE_DB = 15.0                 # a drop or breath is at least this far below the track's median loudness
LOOK_AHEAD_S = 3.0                # the hit is the first moment near full power after the silence
NEAR_FULL_DB = 6.0
EARLY_MAX_S = 3.0                 # an early hit is delayed by padding the head, at most this much


def planned_hit(cue: Cue) -> float | None:
    """Seconds into the cue where the timed sections (planning/score.py) put the big hit or emotional peak."""
    m = HIT_MARK.search(cue.notes)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def find_hit(src: Path, near: float, kind: str = "Hit") -> float | None:
    """When the generated hit or swell arrives, around `near`: the deepest near-silence in the search window, then
    the first moment after it within 6 dB of the loudest point of the next 3 s. None if there is no clear silence.
    Generators drift by seconds and stretch intros, so the moment is found in the audio, not trusted to the prompt."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, "<i2").astype(np.float64)
    hop = int(8000 * ENV_HOP_S)
    if len(a) < hop * 40:
        return None
    rms = np.sqrt(np.convolve(a ** 2, np.ones(hop) / hop, "valid")[::hop]) + 1e-9
    db = np.convolve(20 * np.log10(rms / 32768), np.ones(4) / 4, "same")        # 0.2 s smoothing
    before_s, after_s = SEARCH_S.get(kind, SEARCH_S["Hit"])
    lo, hi = max(0, int((near + before_s) / ENV_HOP_S)), min(len(db), int((near + after_s) / ENV_HOP_S))
    if hi - lo < 4:
        return None
    quiet = lo + int(db[lo:hi].argmin())
    if db[quiet] > float(np.median(db)) - SILENCE_DB:      # no real silence: leave the track as generated
        return None
    j = quiet
    while j < len(db) - 1 and db[j] < db[quiet] + 10:      # end of the silent stretch
        j += 1
    ahead = db[j:j + int(LOOK_AHEAD_S / ENV_HOP_S)]
    if not len(ahead):
        return None
    k = int(np.argmax(ahead >= ahead.max() - NEAR_FULL_DB))
    return round((j + k) * ENV_HOP_S, 2)


def alignment(raw: Path, cue: Cue, seconds: float) -> float:
    """Seconds to drop from the head (negative: silence to add) so the generated hit or peak lands on its shot."""
    planned = planned_hit(cue)
    if planned is None:
        return 0.0
    kind = HIT_MARK.search(cue.notes).group(3)
    found = find_hit(raw, planned, kind)
    if found is None:
        return 0.0
    shift = found - planned
    if shift < -EARLY_MAX_S or abs(shift) <= 0.3 or ffmpeg.duration(raw) - max(shift, 0.0) < 0.7 * seconds:
        return 0.0
    return round(shift, 2)


def fit_to_cue(src: Path, seconds: float, out: Path, start: float = 0.0) -> Path:
    """Exactly `seconds` of 48 kHz stereo from `start` s into the track (negative: that much silence first). A track
    that is long enough, or only a little short, is trimmed or padded at the end with a 1.5 s fade-out; a much
    shorter one (a 30 s clip under a long cue) is looped with crossfades, then trimmed and faded."""
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        have = ffmpeg.duration(src) - start
    except (subprocess.CalledProcessError, KeyError, ValueError):   # an audio format ffmpeg cannot read
        raise GoogleAPIError(f"{src}: ffmpeg cannot decode this generation. Delete it to generate again "
                             "(paid again), or put a track in inbox/music/.") from None
    if have < 1.0:
        raise GoogleAPIError(f"{src}: only {have:.2f} s of audio, too short for a cue. Delete it to generate again.")
    norm = f"aresample={ffmpeg.SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"
    end = f"atrim=0:{seconds:.3f},afade=t=out:st={max(0.0, seconds - FADE_OUT_S):.3f}:d={min(FADE_OUT_S, seconds):.3f}"
    if have + 0.05 >= seconds - max(SHORT_PAD_S, 0.12 * seconds) or start:
        head = f"adelay=delays={round(-start * 1000)}:all=1," if start < 0 else ""
        ffmpeg.run([*(["-ss", f"{start:.3f}"] if start > 0 else []), "-i", str(src), "-af", f"{norm},{head}apad,{end}",
                    "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(out)])
        return out
    start = 0.0                                       # too short to drop a head: loop it whole
    have = ffmpeg.duration(src)
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
        self.styles = load_styles(cfg)
        self._client = client                         # made on the first paid call: cached cues need no sign-in

    @property
    def client(self):
        if self._client is None:
            self._client = google_client({**self.g, "music_location": self.g.get("music_location") or "global"}, "music")
        return self._client

    def prompt(self, cue: Cue) -> str:
        return cue_prompt(cue, self.model, self.chars, self.navarasa, self.styles.get(slot_for(cue)))

    def render_cue(self, cue: Cue, out: Path) -> Path:
        if _find_audio(self.inbox.folder, cue.cue_id):  # a composer's or hand-made track wins, no call
            return self.inbox.render_cue(cue, out)
        prompt = self.prompt(cue)
        raw = cached_raw(self.root, self.model, prompt) or self._generate(cue, prompt)
        seconds = cue_seconds(cue)
        return fit_to_cue(raw, seconds, out, start=alignment(raw, cue, seconds))   # the hit lands on its shot

    def _generate(self, cue: Cue, prompt: str) -> Path:
        what = f"Lyria {cue.cue_id}"
        self.ledger.check(self.price, what)
        modalities = self.g.get("music_response_modalities", ["AUDIO", "TEXT"])   # Lyria 3 refuses AUDIO alone (400)
        config = _types().GenerateContentConfig(response_modalities=list(modalities)) if modalities else None
        response = _call(self.client.models.generate_content, what, model=self.model, contents=prompt, config=config)
        try:
            data, mime = first_audio(response)
        except GoogleAPIError as e:
            self.ledger.record("lyria", cue.cue_id, 0.0, model=self.model, note="no audio returned: assumed not billed")
            raise GoogleAPIError(f"{what}: {e}") from None
        self.ledger.record("lyria", cue.cue_id, self.price, model=self.model, mime=mime)
        return _store(data, mime, cache_dir(self.root), cache_key(self.model, prompt))
