"""Telugu dialogue with ElevenLabs (Eleven v4 and its native Telugu library voices) as a TTS provider.

    providers.tts: elevenlabs
    voice:
      cast: {arjun: f4UZfBDD7d4QR1DcXAMf, meera: o6g54cfKQM0ZoH9yUOBp}   # character_id -> ElevenLabs voice id
      elevenlabs: {model: eleven_v4, language_code: te, stability: 0.35, similarity_boost: 0.8, style: 0.6}
      crowd_provider: gemini          # chanted lines (voice.crowd) need several voices: another provider sings them
    environment: ELEVENLABS_API_KEY (never in a file)

The voice for a line is voice.cast[character_id], else `elevenlabs_voice` in the character's library voice manifest,
else voice.elevenlabs.default_voice. The line's delivery (rasa, volume, notes) becomes Eleven v4 audio tags in front
of the Telugu text. Every take is cached by (model, voice, settings, tagged text) under cache/tts, so a re-render
never pays twice, and logged to the ledger at pricing.elevenlabs.per_1k_chars (list price; a plan's included
characters make the real bill lower).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from ..costing import SpendLedger
from ..post import ffmpeg
from ..post.speech import tighten_pauses
from ..schemas import ScriptBlock
from .base import register

HTTP_FACTORY = None          # tests inject a fake session here
SLEEP = time.sleep
API = "https://api.elevenlabs.io/v1"
DEFAULT_SETTINGS = {"stability": 0.35, "similarity_boost": 0.8, "style": 0.6, "use_speaker_boost": True}
RASA_TAG = {"shringara": "[warm, affectionate]", "hasya": "[amused]", "karuna": "[sad, voice breaking]",
            "raudra": "[furious]", "veera": "[calm, cold, commanding]", "bhayanaka": "[fearful]",
            "bibhatsa": "[disgusted]", "adbhuta": "[in wonder]", "shanta": "[calm]", "vatsalya": "[tender]",
            "bhakti": "[reverent]"}
VOLUME_TAG = {"whisper": "[whispers]", "low": "[quietly]", "loud": "[loud]", "shout": "[shouting]"}


class ElevenLabsError(RuntimeError):
    pass


def tagged_text(line: ScriptBlock) -> str:
    """Eleven v4 audio tags from the line's delivery, then the Telugu text."""
    d = line.delivery
    if d is None:
        return line.text_te.strip()
    if "scream" in (d.notes or "").lower():
        tags = ["[screams]"]
    else:
        tags = [t for t in (RASA_TAG.get(d.rasa, ""), VOLUME_TAG.get(d.volume, "")) if t]
    return " ".join([*tags, line.text_te.strip()]).strip()


@register("tts", "elevenlabs")
class ElevenLabsTTS:
    def __init__(self, voices: dict[str, dict], cfg: dict, pricing: dict, ledger: SpendLedger, http=None,
                 crowd=None, cache_dir: Path | None = None):
        v = cfg.get("voice", {}) or {}
        e = v.get("elevenlabs", {}) or {}
        self.cast: dict[str, str] = dict(v.get("cast") or {})
        self.voices = voices                               # library voice manifests by asset ref
        self.model = e.get("model", "eleven_v4")
        self.language = e.get("language_code", "te")
        self.default_voice = e.get("default_voice")
        self.settings = {**DEFAULT_SETTINGS, **{k: e[k] for k in DEFAULT_SETTINGS if k in e}}
        self.output_format = e.get("output_format", "mp3_44100_128")
        self.api_key_env = e.get("api_key_env", "ELEVENLABS_API_KEY")
        self.max_pause_s = v.get("max_pause_s", 0.6)
        self.per_1k = float((pricing.get("elevenlabs") or {}).get("per_1k_chars", 0.18))
        self.ledger = ledger
        self.cache_dir = cache_dir or ledger.path.parent.parent / "cache" / "tts"
        self.crowd = crowd
        self._http = http

    # -- http

    def http(self):
        if self._http is None:
            if HTTP_FACTORY is not None:
                self._http = HTTP_FACTORY()
            else:
                import requests
                key = os.environ.get(self.api_key_env, "").strip()
                if not key:
                    raise ElevenLabsError(f"voices: the ElevenLabs API key is missing; set {self.api_key_env} in the "
                                          "environment (never in a file or the chat).")
                s = requests.Session()
                s.headers.update({"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
                self._http = s
        return self._http

    def _post(self, voice_id: str, body: dict, what: str) -> bytes:
        url = f"{API}/text-to-speech/{voice_id}"
        last = None
        for attempt in range(4):
            try:
                resp = self.http().post(url, json=body, params={"output_format": self.output_format}, timeout=180)
            except Exception as exc:                        # connection refused by the proxy, DNS, timeout
                last = f"{type(exc).__name__}: {exc}"
                SLEEP(2 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status_code}"
                SLEEP(3 * (attempt + 1))
                continue
            if resp.status_code in (400, 422) and "language_code" in body:
                body = {k: v for k, v in body.items() if k != "language_code"}   # some models refuse the hint
                continue
            if resp.status_code >= 400:
                raise ElevenLabsError(f"{what}: HTTP {resp.status_code}: {getattr(resp, 'text', '')[:300]}")
            return resp.content
        raise ElevenLabsError(f"{what}: gave up after retries ({last})")

    # -- voices

    def voice_id(self, line: ScriptBlock, voice_ref: str | None) -> str:
        cid = line.character_id or ""
        vid = self.cast.get(cid) or (self.voices.get(voice_ref or "", {}) or {}).get("elevenlabs_voice") \
            or self.default_voice
        if not vid:
            raise ElevenLabsError(f"voices: no ElevenLabs voice for '{cid}': add voice.cast.{cid}: <voice id> to the "
                                  "project's pipeline.yaml (or elevenlabs_voice to its library voice manifest).")
        return str(vid)

    # -- provider API

    def cache_key(self, line: ScriptBlock, voice_ref: str | None = None) -> str:
        """The cache name of this line's take: the model, the voice, the settings and the tagged text. The cost gate
        asks it too, so a take already made is never priced again."""
        vid = self.voice_id(line, voice_ref)
        return hashlib.sha1(json.dumps([self.model, vid, self.settings, tagged_text(line)],
                                       sort_keys=True).encode()).hexdigest()[:16]

    def synthesize(self, line: ScriptBlock, voice_ref: str | None, out: Path) -> float:
        vid = self.voice_id(line, voice_ref)
        text = tagged_text(line)
        body = {"text": text, "model_id": self.model, "voice_settings": self.settings,
                **({"language_code": self.language} if self.language else {})}
        key = self.cache_key(line, voice_ref)
        cached = self.cache_dir / f"el_{key}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            usd = round(len(text) * self.per_1k / 1000, 5)
            self.ledger.check(usd, f"ElevenLabs {line.block_id}")
            audio = self._post(vid, body, f"ElevenLabs {line.block_id}")
            raw = out.with_suffix(".el.mp3")
            raw.write_bytes(audio)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            ffmpeg.run(["-i", str(raw), "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(cached)])
            raw.unlink()
            self.ledger.record("elevenlabs", line.block_id, usd, characters=len(text), model=self.model, voice=vid,
                               note=f"list price {self.per_1k}/1K chars; a plan's included characters cost less")
        shutil.copy2(cached, out)
        if not self.max_pause_s:
            return ffmpeg.duration(out)
        return tighten_pauses(out, self.max_pause_s, keep_s=min(0.3, self.max_pause_s))

    def can_chant(self, line: ScriptBlock) -> bool:
        return self.crowd is not None and hasattr(self.crowd, "synthesize_crowd")

    def synthesize_crowd(self, line: ScriptBlock, voice_ref: str | None, out: Path, voices: int) -> float:
        if not self.can_chant(line):
            raise ElevenLabsError("voices: a chant needs voice.crowd_provider (e.g. gemini) next to elevenlabs.")
        return self.crowd.synthesize_crowd(line, voice_ref, out, voices)



# -- casting voices: the account's voices, and one for each speaking character by gender, age and timbre

AGES = ["child", "young", "middle_aged", "old"]
PITCH_WORDS = {"high": ("high", "bright", "light", "young", "child"), "mid": (), "low": ("deep", "low", "husky",
                                                                                         "gravelly", "raspy", "rich")}


def account_voices(http=None, api_key_env: str = "ELEVENLABS_API_KEY") -> list[dict]:
    """The voices this ElevenLabs account can speak with (its own and the ones it added from the voice library), as
    GET /v1/voices lists them: voice_id, name, labels (gender, age, accent, description), description."""
    s = http or (HTTP_FACTORY() if HTTP_FACTORY is not None else None)
    if s is None:
        import requests
        key = os.environ.get(api_key_env, "").strip()
        if not key:
            raise ElevenLabsError(f"voices: the ElevenLabs API key is missing; set {api_key_env} in the environment.")
        s = requests.Session()
        s.headers.update({"xi-api-key": key, "Accept": "application/json"})
    resp = s.get(f"{API}/voices", timeout=60)
    if resp.status_code != 200:
        raise ElevenLabsError(f"voices: listing the account's voices: HTTP {resp.status_code}")
    return [{"voice_id": v["voice_id"], "name": v.get("name") or v["voice_id"], "labels": v.get("labels") or {},
             "description": v.get("description") or ""} for v in (resp.json() or {}).get("voices", []) if v.get("voice_id")]


def age_band(age: int) -> str:
    return "child" if age < 13 else "young" if age < 30 else "middle_aged" if age < 55 else "old"


def _band(label: str) -> str | None:
    """An age band from a label or a description, word by word ("golden" is not "old")."""
    words = set("".join(c if c.isalpha() else " " for c in str(label or "").lower()).split())
    for band, said in (("child", {"child", "kid", "kids", "children"}), ("young", {"young"}),
                       ("middle_aged", {"middle"}), ("old", {"old", "elder", "elderly", "senior"})):
        if words & said:
            return band
    return None


def want_for(spec, animal: bool = False) -> dict:
    """What a character's voice should be, from the story bible: gender, an age band (an animal's comes from its
    pitch: a squeaky one sounds young, a rumbling one old), and the words of its pitch and timbre."""
    pitch = getattr(spec.voice, "pitch", "mid")
    age = ("young" if pitch == "high" else "old" if pitch == "low" else "middle_aged") if animal else \
        age_band(spec.physical.age)
    words = [w for w in "".join(c if c.isalpha() else " " for c in getattr(spec.voice, "timbre", "").lower()).split()
             if len(w) >= 4] + list(PITCH_WORDS.get(pitch, ()))
    return {"gender": spec.physical.gender.lower(), "age": age, "words": list(dict.fromkeys(words))}


def voice_score(want: dict, voice: dict) -> tuple[int, list[str]]:
    labels = {k: str(v).lower() for k, v in (voice.get("labels") or {}).items()}
    text = " ".join((labels.get("description", ""), labels.get("use_case", ""), str(voice.get("description", "")).lower(),
                     str(voice.get("name", "")).lower()))
    score, why = 0, []
    gender = labels.get("gender", "")
    if gender and want["gender"] in ("male", "female"):
        if gender == want["gender"]:
            score, why = score + 4, why + [gender]
        else:
            score -= 6
    band = _band(labels.get("age", "")) or _band(text)
    if band:
        d = abs(AGES.index(band) - AGES.index(want["age"]))
        score += {0: 3, 1: 1}.get(d, -2)
        if d == 0:
            why.append(band.replace("_", " "))
    for w in want["words"]:
        if w in text:
            score, why = score + 1, why + [w]
    if "indian" in labels.get("accent", ""):
        score, why = score + 1, why + ["Indian accent"]
    return score, why


def pick_voices(wants: list[tuple[str, dict]], voices: list[dict], taken=()) -> dict[str, dict]:
    """One voice per character, in the order given (the characters with most lines first): the best-scoring voice no
    other character has, and a shared one only when the account has fewer voices than speakers."""
    used, picks = set(taken), {}
    for cid, want in wants:
        if not voices:
            break
        scored = sorted(((voice_score(want, v), v) for v in voices),
                        key=lambda sv: (sv[1]["voice_id"] in used, -sv[0][0], sv[1]["name"]))
        (score, why), best = scored[0]
        picks[cid] = {"voice_id": best["voice_id"], "name": best["name"], "why": ", ".join(why) or "closest match",
                      "shared": best["voice_id"] in used}
        used.add(best["voice_id"])
    return picks
