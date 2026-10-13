"""Google AI providers (google-genai SDK): Veo video, Gemini keyframe images, Gemini Telugu TTS.

    pip install google-genai
    export GEMINI_API_KEY=...        # Gemini API key, project with billing (docs/GOOGLE_CLOUD_SETUP.md)
    # or Vertex AI: set google.vertex: true and GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION, with
    # GOOGLE_APPLICATION_CREDENTIALS_JSON (a service account key's JSON text) or the usual Google sign-in

Video modes (config google.veo_mode):
  keyframe  - Gemini draws the first frame from the characters' reference images, then Veo
              animates it (image-to-video). Clips can be 4/6/8 s, and the still is a cheap
              checkpoint before paying for video.
  reference - Veo gets up to 3 reference images directly (asset references). Clips are 8 s.

Every paid call is checked against the budget and written to reports/cost.jsonl. Every
generated clip is saved in inbox/video/ (<shot>.mp4, <shot>_2.mp4 ...), so a re-run, a
crash or a budget stop never pays twice for the same attempt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import wave
from pathlib import Path
from typing import Any, Callable

from ..costing import SpendLedger
from ..post import ffmpeg
from ..schemas import CastLock, Outline, ScriptBlock, Shot, StoryBible
from .base import RenderContext, register

CLIENT_FACTORY: Callable[[dict], Any] | None = None    # tests inject a fake client here


class ShotFiltered(RuntimeError):
    """The provider's safety filter refused this shot; it goes to the review queue."""


class GoogleSetupError(RuntimeError):
    """The Google SDK or credentials are missing."""


class GoogleAPIError(RuntimeError):
    """A Google API call failed (bad model name, quota, invalid argument). The run can be resumed."""


VERTEX_HINT = ("This key type is refused by the Gemini API endpoint. Use the same key through Vertex AI: set "
               "google.vertex: true in the project's pipeline.yaml (check with `nraiorbit google models --vertex`; "
               "needs aiplatform.googleapis.com and the Vertex AI API enabled).")
FREE_TIER_HINT = ("The key's Google project is on the Gemini API free tier, which does not include this model, so "
                  "nothing was charged. Link that project to a paid billing account: in AI Studio (API keys page) "
                  "the project must show a paid tier, not 'Free' (docs/GOOGLE_CLOUD_SETUP.md steps 2-3). Then re-run.")

PREPAY_HINT = ("The key's Google project uses Gemini API prepaid billing and its credit is used up, so nothing was "
               "charged. Add credit in AI Studio (Billing), or use Google Cloud credit through Vertex AI "
               "(google.vertex: true with GOOGLE_APPLICATION_CREDENTIALS_JSON, GOOGLE_CLOUD_PROJECT and "
               "GOOGLE_CLOUD_LOCATION). Then re-run.")


def _call(fn, what: str, **kwargs):
    try:
        return fn(**kwargs)
    except Exception as e:  # SDK errors live in google.genai.errors; re-raise everything else unchanged
        if type(e).__module__.startswith("google."):
            if "ACCESS_TOKEN_TYPE_UNSUPPORTED" in str(e):
                hint = VERTEX_HINT
            elif "free_tier" in str(e):
                hint = FREE_TIER_HINT
            elif "prepayment credits" in str(e):
                hint = PREPAY_HINT
            else:
                hint = ("If the model name is wrong, check `nraiorbit google models` and set it under google: in "
                        "the project's pipeline.yaml. Re-run to resume.")
            raise GoogleAPIError(f"{what}: {e}\n{hint}") from None
        raise


def _transient(e: Exception) -> bool:
    """A brief network or server hiccup worth polling through: 429/5xx, or a dropped connection."""
    code = getattr(e, "code", None)
    if isinstance(code, int) and (code == 429 or code >= 500):
        return True
    return type(e).__module__.split(".")[0] in ("httpx", "httpcore") or isinstance(e, ConnectionError)


def google_client(google_cfg: dict, role: str = ""):
    """role ("veo", "image", "tts"): on Vertex AI, google.<role>_location picks that model's region, because one
    region may not serve all of them (Veo is regional, e.g. us-central1; the pro image model is served from global)."""
    if CLIENT_FACTORY is not None:
        return CLIENT_FACTORY(google_cfg)
    try:
        from google import genai
    except ImportError:
        raise GoogleSetupError("google-genai is not installed: pip install google-genai") from None
    try:
        # Images and voices retry a brief 429/5xx (a refused call is not billed). Veo submissions never retry
        # automatically: a retry after an ambiguous error could start, and pay for, a second job.
        extra = {"http_options": _types().HttpOptions(retry_options=_types().HttpRetryOptions(
            attempts=5, initial_delay=2, max_delay=60))} if role in ("image", "tts") else {}
        if google_cfg.get("vertex"):
            return genai.Client(vertexai=True, **extra,
                                **vertex_auth(google_cfg.get(f"{role}_location") if role else None))
        return genai.Client(**extra)
    except ValueError as e:
        raise GoogleSetupError(
            f"{e}\nSet GEMINI_API_KEY (Gemini API key on a project with billing; see docs/GOOGLE_CLOUD_SETUP.md), "
            "or set google.vertex: true "
            "with GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION for Vertex AI.") from None


def vertex_auth(location: str | None = None) -> dict:
    """Vertex AI sign-in from the environment: a service account key's JSON text in
    GOOGLE_APPLICATION_CREDENTIALS_JSON (handy where only env vars can be set), else the SDK's default
    (GOOGLE_APPLICATION_CREDENTIALS file or gcloud login). Passing project/location explicitly keeps the SDK
    from switching to API-key express mode just because GEMINI_API_KEY is also set."""
    kwargs: dict = {}
    for name, key in (("GOOGLE_CLOUD_PROJECT", "project"), ("GOOGLE_CLOUD_LOCATION", "location")):
        if os.environ.get(name):
            kwargs[key] = os.environ[name]
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
    if raw:
        try:
            info = json.loads(raw)
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except (ValueError, KeyError) as e:
            raise GoogleSetupError(f"GOOGLE_APPLICATION_CREDENTIALS_JSON is not a valid service account key "
                                   f"({type(e).__name__}). Paste the whole downloaded JSON file.") from None
        kwargs["credentials"] = creds
        kwargs.setdefault("project", info.get("project_id"))
    if location:
        kwargs["location"] = location
    kwargs.setdefault("location", "us-central1")
    return kwargs


def _types():
    from google.genai import types

    return types


def _image_part(path: Path):
    types = _types()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


def _first_inline(response) -> tuple[bytes, str]:
    for cand in response.candidates or []:
        for part in (cand.content.parts if cand.content else None) or []:
            if part.inline_data is not None and part.inline_data.data:
                return part.inline_data.data, part.inline_data.mime_type or ""
    reason = getattr(response.candidates[0], "finish_reason", None) if response.candidates else None
    feedback = getattr(response, "prompt_feedback", None)
    raise ShotFiltered(f"no media returned (finish_reason={reason}, prompt_feedback={feedback})")


# ------------------------------------------------------------------------------ keyframes


class GeminiImages:
    """Gemini image generation/editing, used for keyframes and identity packs."""

    def __init__(self, client, google_cfg: dict, pricing: dict, ledger: SpendLedger):
        self.client = client
        self.model = google_cfg.get("image_model", "gemini-2.5-flash-image")
        self.price = pricing.get("gemini", {}).get("image_per_image", 0.04)
        self.ledger = ledger

    def generate(self, prompt: str, out: Path, *, refs: list[Path] = (), aspect_ratio: str = "16:9",
                 item: str = "image") -> Path:
        types = _types()
        self.ledger.check(self.price, item)
        response = _call(
            self.client.models.generate_content, f"Gemini image ({item})",
            model=self.model,
            contents=[*(_image_part(r) for r in refs), prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio=aspect_ratio)),
        )
        self.ledger.record("gemini_image", item, self.price, model=self.model)
        data, mime = _first_inline(response)
        out.parent.mkdir(parents=True, exist_ok=True)
        raw = out.with_suffix(".jpg" if "jpeg" in mime else ".png")
        raw.write_bytes(data)
        if raw != out:
            ffmpeg.run(["-i", str(raw), str(out)])
            raw.unlink()
        return out


def keyframe_prompt(shot: Shot, base_prompt: str, n_refs_by_char: list[tuple[str, int]]) -> str:
    who = []
    i = 1
    for name, n in n_refs_by_char:
        span = f"image {i}" if n == 1 else f"images {i}-{i + n - 1}"
        who.append(f"{name} is the person in reference {span}: keep exactly the same face, skin tone, hair and build")
        i += n
    lead = "; ".join(who)
    return (f"Create one photorealistic 16:9 film still, the first frame of this shot. {lead + '. ' if lead else ''}"
            f"{base_prompt} Do not add any text, captions or borders.")


# ------------------------------------------------------------------------------ video


@register("video", "veo")
class VeoVideo:
    def __init__(self, project_root: Path, library, cfg: dict, pricing: dict, ledger: SpendLedger,
                 navarasa: dict, client=None):
        from ..shotpack import _reference_images  # local: shotpack imports costing, avoid cycles at import

        self._refs_for = _reference_images
        self.root = project_root
        self.library = library
        self.g = cfg.get("google", {})
        self.pricing = pricing
        self.ledger = ledger
        self.navarasa = navarasa
        self.client = client or google_client(self.g, "veo")
        self.images = GeminiImages(client or google_client(self.g, "image"), self.g, pricing, ledger)
        self.bible = StoryBible.model_validate_json((project_root / "01_story_bible.json").read_text(encoding="utf-8"))
        self.outline = Outline.model_validate_json((project_root / "02_outline.json").read_text(encoding="utf-8"))
        self.scripts: dict[str, dict] = {}

    # -- helpers

    def _speaker(self, shot: Shot, scene_id: str) -> str | None:
        if scene_id not in self.scripts:
            path = self.root / "03_scenes" / f"{scene_id}.json"
            blocks = json.loads(path.read_text(encoding="utf-8"))["blocks"] if path.exists() else []
            self.scripts[scene_id] = {b["block_id"]: b for b in blocks}
        blocks = self.scripts[scene_id]
        names = {c.id: c.name_en for c in self.bible.characters}
        for b in shot.block_ids:
            blk = blocks.get(b)
            if blk and blk["kind"] == "dialogue" and blk["character_id"] in shot.characters_in_frame:
                return names.get(blk["character_id"])
        return None

    def _refs(self, shot: Shot, lock: CastLock, limit: int) -> list[tuple[str, list[Path]]]:
        names = {c.id: c.name_en for c in self.bible.characters}
        out, total = [], 0
        for i, cid in enumerate(shot.characters_in_frame):
            costume = shot.costume_ids[i] if i < len(shot.costume_ids) else None
            imgs = self._refs_for(cid, costume, lock, self.library)
            if not imgs:
                raise FileNotFoundError(
                    f"{shot.shot_id}: no reference images for '{cid}'. Run `nraiorbit identity build {self.root.name}` "
                    "or add images to the character's library refs/ folder.")
            share = max(1, (limit - total) // max(1, len(shot.characters_in_frame) - i))
            picked = imgs[:share]
            total += len(picked)
            out.append((names.get(cid, cid), picked))
        return out

    def _clip_seconds(self, shot: Shot, mode: str) -> int:
        from ..costing import veo_clip_seconds

        return veo_clip_seconds(shot.duration_s, self.pricing, self.g.get("veo_resolution", "720p"), mode,
                                bool(shot.characters_in_frame))

    def _price_per_second(self) -> float:
        return self.pricing["veo"]["api_per_second"][self.g.get("veo_tier", "fast")][self.g.get("veo_resolution", "720p")]

    def _poll(self, operation):
        try:
            return self.client.operations.get(operation=operation)
        except Exception as e:
            if _transient(e):                             # the job keeps running on Google's side: ask again later
                return operation
            raise

    # -- provider API

    def render_shot(self, shot: Shot, out: Path, ctx: RenderContext) -> Path:
        from ..shotpack import NEGATIVE, clips_for, inbox_dir, veo_prompt

        attempt = max(1, ctx.seed)
        existing = clips_for(self.root, shot.shot_id)
        if attempt <= len(existing):                     # already paid for: reuse
            return ffmpeg.conform_clip(existing[attempt - 1], out, shot.duration_s, ctx.resolution, ctx.fps)

        dest = inbox_dir(self.root, "video") / (f"{shot.shot_id}.mp4" if attempt == 1 else f"{shot.shot_id}_{attempt}.mp4")
        pending = dest.parent / ".pending" / f"{dest.stem}.json"
        types = _types()
        if pending.exists():                              # submitted and paid earlier; collect it, don't pay again
            job = json.loads(pending.read_text(encoding="utf-8"))
            operation = types.GenerateVideosOperation(name=job["operation"])
            model, mode, seconds, cost = job["model"], job["mode"], job["seconds"], job["usd"]
        else:
            scene = next(s for s in self.outline.scenes if s.scene_id == ctx.scene_id)
            prompt = veo_prompt(shot, scene, self.bible, ctx.cast, self.navarasa, self._speaker(shot, ctx.scene_id))
            mode = self.g.get("veo_mode", "keyframe")
            seconds = self._clip_seconds(shot, mode)
            cost = seconds * self._price_per_second()
            self.ledger.check(cost, f"Veo {shot.shot_id} ({seconds} s)")

            config = dict(duration_seconds=seconds, aspect_ratio="16:9", resolution=self.g.get("veo_resolution", "720p"),
                          negative_prompt=NEGATIVE, number_of_videos=1)
            if self.g.get("vertex"):                      # Veo audio is dropped in the edit (dialogue is TTS); on
                config["generate_audio"] = bool(self.g.get("veo_audio", False))  # Vertex it is billed extra
            if self.g.get("person_generation"):
                config["person_generation"] = self.g["person_generation"]
            model = self.g.get("veo_model", "veo-3.1-fast-generate-preview")
            kwargs: dict[str, Any] = {"model": model, "prompt": prompt}

            refs = self._refs(shot, ctx.cast, 3) if shot.characters_in_frame else []
            if refs and mode == "reference":
                config["reference_images"] = [
                    types.VideoGenerationReferenceImage(image=types.Image.from_file(location=str(p)),
                                                        reference_type=types.VideoGenerationReferenceType.ASSET)
                    for _, paths in refs for p in paths]
            elif refs:                                   # keyframe mode; shots without leads go text-to-video
                keyframe = self.root / "06_keyframes" / shot.shot_id / f"take_{attempt}.png"
                if not keyframe.exists():
                    self.images.generate(keyframe_prompt(shot, prompt, [(n, len(p)) for n, p in refs]), keyframe,
                                         refs=[p for _, paths in refs for p in paths], item=f"keyframe {shot.shot_id}")
                kwargs["image"] = types.Image.from_file(location=str(keyframe))

            operation = _call(self.client.models.generate_videos, f"Veo {shot.shot_id}", **kwargs,
                              config=types.GenerateVideosConfig(**config))
            # Logged as soon as the job exists, so a crash, timeout or Ctrl-C while it runs still counts toward the
            # budget, and the job's name is kept so the next run collects the clip instead of paying for a new one.
            self.ledger.record("veo", shot.shot_id, cost, seconds=seconds, model=model, mode=mode, attempt=attempt)
            if getattr(operation, "name", None):
                pending.parent.mkdir(parents=True, exist_ok=True)
                pending.write_text(json.dumps({"operation": operation.name, "model": model, "mode": mode,
                                               "seconds": seconds, "usd": cost}), encoding="utf-8")

        timeout = self.g.get("timeout_seconds", 900)
        deadline = time.time() + timeout
        while not operation.done:
            if time.time() > deadline:
                raise GoogleAPIError(f"Veo {shot.shot_id}: still running after {timeout} s. It is already paid for and "
                                     "will be collected, not paid again, when you re-run.")
            time.sleep(self.g.get("poll_seconds", 10))
            try:
                operation = _call(self._poll, f"Veo {shot.shot_id} status", operation=operation)
            except GoogleAPIError as e:
                if not pending.exists():
                    raise
                raise GoogleAPIError(f"{e}\nIf Google no longer knows this job, delete {pending} to submit the shot "
                                     "again (that is paid again).") from None

        def not_billed(**why) -> None:                    # the job ended without a clip
            self.ledger.record("veo", shot.shot_id, -cost, seconds=-seconds, note="no clip: not billed", **why)
            pending.unlink(missing_ok=True)

        if operation.error:
            err = operation.error if isinstance(operation.error, dict) else {"message": str(operation.error)}
            message = str(err.get("message", err))
            if err.get("code") == 3 or any(w in message.lower() for w in ("guideline", "safety", "policy")):
                not_billed(filtered=message)
                raise ShotFiltered(f"Veo refused {shot.shot_id}: {message}")
            not_billed(error=message)
            raise GoogleAPIError(f"Veo {shot.shot_id} failed: {message}\nRe-run to try this shot again.")
        result = operation.response or operation.result
        videos = getattr(result, "generated_videos", None) or []
        if not videos:
            reasons = getattr(result, "rai_media_filtered_reasons", None)
            not_billed(filtered=str(reasons))
            raise ShotFiltered(f"Veo filtered {shot.shot_id}: {reasons}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        video = videos[0].video
        if not self.g.get("vertex"):
            _call(self.client.files.download, f"Veo {shot.shot_id} download", file=video)
        video.save(str(dest))
        pending.unlink(missing_ok=True)
        return ffmpeg.conform_clip(dest, out, shot.duration_s, ctx.resolution, ctx.fps)


# ------------------------------------------------------------------------------ Telugu TTS

GEMINI_TONE = {
    "shringara": "warmly and affectionately", "hasya": "playfully, amused", "karuna": "sadly, close to tears",
    "raudra": "with controlled anger", "veera": "confidently, with quiet authority", "bhayanaka": "fearfully",
    "bibhatsa": "with disgust", "adbhuta": "in wonder", "shanta": "calmly", "vatsalya": "tenderly",
    "bhakti": "reverently",
}
GEMINI_PACE = {"very_slow": "very slowly", "slow": "slowly", "medium": "", "fast": "quickly", "very_fast": "very quickly"}
GEMINI_VOLUME = {"whisper": "in a whisper", "low": "softly", "normal": "", "loud": "loudly", "shout": "shouting"}


def tts_instruction(line: ScriptBlock) -> str:
    d = line.delivery
    style = ", ".join(x for x in (GEMINI_TONE.get(d.rasa, "") if d else "", GEMINI_PACE.get(d.pace, "") if d else "",
                                  GEMINI_VOLUME.get(d.volume, "") if d else "") if x)
    note = f" ({d.notes})" if d and d.notes else ""
    return f"Say in natural spoken Telugu, {style or 'naturally'}{note}: {line.text_te}"


@register("tts", "gemini")
class GeminiTTS:
    def __init__(self, voices: dict[str, dict], cfg: dict, pricing: dict, ledger: SpendLedger, client=None,
                 cache_dir: Path | None = None):
        self.cache_dir = cache_dir or ledger.path.parent.parent / "cache" / "tts"
        self.g = cfg.get("google", {})
        self.voices = voices
        self.client = client or google_client(self.g, "tts")
        self.ledger = ledger
        self.per_minute = pricing.get("gemini", {}).get("tts_per_minute", 0.02)

    def synthesize(self, line: ScriptBlock, voice_ref: str | None, out: Path) -> float:
        types = _types()
        voice = self.voices.get(voice_ref or "", {})
        voice_name = voice.get("gemini_voice") or ("Charon" if voice.get("gender") == "male" else "Kore")
        model = self.g.get("tts_model", "gemini-2.5-flash-preview-tts")
        instruction = tts_instruction(line)
        key = hashlib.sha1(json.dumps([model, voice_name, instruction]).encode("utf-8")).hexdigest()[:16]
        cached = self.cache_dir / f"{key}.wav"
        if cached.exists():                               # same line, voice and delivery: already paid
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, out)
            return ffmpeg.duration(out)
        self.ledger.check(self.per_minute * 0.25, f"TTS {line.block_id}")
        speech = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)),
            **({"language_code": self.g["tts_language_code"]} if self.g.get("tts_language_code") else {}))
        response = _call(self.client.models.generate_content, f"Gemini TTS {line.block_id}",
                         model=model, contents=instruction,
                         config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=speech))
        pcm, mime = _first_inline(response)
        rate = 24000
        for piece in mime.split(";"):
            if piece.strip().startswith("rate="):
                rate = int(piece.split("=")[1])
        out.parent.mkdir(parents=True, exist_ok=True)
        raw = out.with_suffix(".pcm.wav")
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        ffmpeg.run(["-i", str(raw), "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(out)])
        raw.unlink()
        seconds = ffmpeg.duration(out)
        self.ledger.record("gemini_tts", line.block_id, self.per_minute * seconds / 60, seconds=round(seconds, 2),
                           voice=voice_name)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, cached)
        return seconds
