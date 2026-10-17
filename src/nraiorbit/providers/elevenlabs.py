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

    def synthesize(self, line: ScriptBlock, voice_ref: str | None, out: Path) -> float:
        vid = self.voice_id(line, voice_ref)
        text = tagged_text(line)
        body = {"text": text, "model_id": self.model, "voice_settings": self.settings,
                **({"language_code": self.language} if self.language else {})}
        key = hashlib.sha1(json.dumps([self.model, vid, self.settings, text], sort_keys=True).encode()).hexdigest()[:16]
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
