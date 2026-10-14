"""Automatic POC run against a fake Google client: Veo, Gemini keyframes, Gemini TTS, budget, filters, caching."""

import json
import shutil
from types import SimpleNamespace

import numpy as np
import pytest

from nraiorbit.config import REPO_ROOT, deep_merge, load_pipeline_config
from nraiorbit.costing import SpendLedger, load_pricing, preview_run
from nraiorbit.identity import build_identity_pack
from nraiorbit.library.registry import AssetRegistry
from nraiorbit.pipeline import Pipeline, PipelineBlocked
from nraiorbit.poc import create_poc
from nraiorbit.post import ffmpeg
from nraiorbit.providers import google_ai
from nraiorbit.providers.google_ai import GeminiImages, tts_instruction
from nraiorbit.schemas import ShotList

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
pytest.importorskip("google.genai")


class HiccupError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} UNAVAILABLE")
        self.code = code


class FakeGoogle:
    """Mimics the google-genai surface the providers use."""

    def __init__(self, tmp, block_if=lambda prompt: False):
        self.clip = tmp / "veo_clip.mp4"
        ffmpeg.run(["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(self.clip)])
        png = tmp / "img.png"
        ffmpeg.run(["-f", "lavfi", "-i", "color=c=0x885533:s=320x240", "-frames:v", "1", str(png)])
        self.png = png.read_bytes()
        t = np.arange(24000) / 24000
        self.pcm = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes()
        self.block_if = block_if
        self.poll_failures, self.op_error, self.poll_configs = 0, None, []
        self.video_calls, self.image_calls, self.tts_calls = [], [], []
        self.models = SimpleNamespace(generate_videos=self._videos, generate_content=self._content, list=lambda: [])
        self.operations = SimpleNamespace(get=self._get)
        self.files = SimpleNamespace(download=lambda file: None)

    def _videos(self, *, model, prompt=None, image=None, config=None):
        self.video_calls.append({"model": model, "prompt": prompt, "image": image, "config": config})
        return SimpleNamespace(name=f"projects/p/locations/us-central1/publishers/google/models/{model}/operations/"
                                    f"{len(self.video_calls)}",
                               done=False, error=None, response=None, result=None, blocked=self.block_if(prompt))

    def _get(self, operation, config=None):   # same keywords as google.genai Operations.get
        self.poll_configs.append(config)
        if self.poll_failures:           # a brief server hiccup while polling
            self.poll_failures -= 1
            raise HiccupError(503)
        blocked = getattr(operation, "blocked", False)    # a resumed job is a real GenerateVideosOperation
        if self.op_error:
            return SimpleNamespace(done=True, error=self.op_error, result=None, response=None)
        clip = self.clip
        videos = [] if blocked else [SimpleNamespace(video=SimpleNamespace(save=lambda p: shutil.copy(clip, p)))]
        return SimpleNamespace(done=True, error=None, result=None, blocked=blocked, response=SimpleNamespace(
            generated_videos=videos, rai_media_filtered_reasons=["test filter"] if blocked else None))

    def _content(self, *, model, contents, config):
        audio = config.response_modalities == ["AUDIO"]
        (self.tts_calls if audio else self.image_calls).append({"model": model, "contents": contents, "config": config})
        blob = SimpleNamespace(data=self.pcm if audio else self.png,
                               mime_type="audio/L16;codec=pcm;rate=24000" if audio else "image/png")
        part = SimpleNamespace(inline_data=blob)
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")],
                               prompt_feedback=None)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    lib_root = tmp_path / "library"
    # identity images must not land in the repo; skip images from a real local run (gitignored media)
    shutil.copytree(REPO_ROOT / "library", lib_root, ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.jpeg"))
    library = AssetRegistry.load(lib_root)
    root = create_poc(tmp_path / "godavari5")
    fake = FakeGoogle(tmp_path)
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", lambda cfg: fake)

    def make(budget=500.0, only=None, block_if=None):
        if block_if is not None:
            fake.block_if = block_if
        cfg = deep_merge(load_pipeline_config(root), {
            "film": {"resolution": [320, 180]}, "google": {"poll_seconds": 0},
            "budget": {"max_usd": budget}, "render": {"only": only or []}})
        return Pipeline(root, llm=None, cfg=cfg, library=library, log=lambda m: None), cfg

    pipeline, cfg = make()
    pipeline.run(until="cast")
    images = GeminiImages(fake, cfg["google"], load_pricing(root), pipeline.ledger)
    build_identity_pack(json_bible(root), pipeline._lock(), library, images, log=lambda m: None)
    return SimpleNamespace(root=root, fake=fake, make=make, library=library, lib_root=lib_root)


def json_bible(root):
    from nraiorbit.schemas import StoryBible

    return StoryBible.model_validate_json((root / "01_story_bible.json").read_text())


def _selected_shots(root, trailer_only):
    shots = []
    for path in sorted((root / "04_shots").glob("*.json")):
        for s in ShotList.model_validate_json(path.read_text()).shots:
            if not trailer_only or s.trailer_candidate or s.punch_dialogue:
                shots.append(s)
    return shots


def test_identity_pack_written_to_library(setup):
    refs = sorted((setup.lib_root / "characters" / "arjun_hero" / "refs").rglob("*.png"))
    assert [p.name for p in refs].count("01_full_body.png") == 2          # two costumes
    assert any(p.name == "01_front.png" for p in refs)
    # 3 characters: front + three-quarter + one per costume (2 + 2 + 1)
    assert len(setup.fake.image_calls) == 2 * 3 + 5
    three_q = next(c for c in setup.fake.image_calls if "three-quarter" in str(c["contents"][-1]))
    assert len(three_q["contents"]) == 2                                   # reference portrait + prompt


def test_trailer_only_run_then_full_run_reuses_paid_clips(setup):
    root, fake = setup.root, setup.fake
    images_before = len(fake.image_calls)
    pipeline, cfg = setup.make(only=["trailer"])
    pre = preview_run(root, cfg, load_pricing(root))
    trailer_shots = _selected_shots(root, trailer_only=True)
    assert pre["shots"] == len(trailer_shots) and pre["cached_shots"] == 0

    pipeline.run(until="render")
    assert len(fake.video_calls) == len(trailer_shots)
    with_leads = [s for s in trailer_shots if s.characters_in_frame]
    assert len(fake.image_calls) - images_before == len(with_leads)     # one keyframe per shot with a lead
    keyframed = [c for c in fake.video_calls if c["image"] is not None]
    assert len(keyframed) == len(with_leads)
    cfg0 = fake.video_calls[0]["config"]
    assert cfg0.aspect_ratio == "16:9" and cfg0.resolution == "1080p" and cfg0.duration_seconds == 8  # 1080p: 8 s
    assert "Arjun" in next(c["prompt"] for c in fake.video_calls if "Arjun" in c["prompt"])

    ledger = SpendLedger(root / "reports" / "cost.jsonl", None)
    veo_seconds = sum(r.get("seconds", 0) for r in ledger.records() if r["provider"] == "veo")
    assert ledger.summary()["veo"] == pytest.approx(veo_seconds * 0.40)          # Veo 3.1 standard
    assert (root / "inbox" / "video" / f"{trailer_shots[0].shot_id}.mp4").exists()
    timeline = json.loads((root / "10_edit" / "timeline.json").read_text())
    assert timeline["only"] == ["trailer"]
    tts_calls = len(fake.tts_calls)
    assert tts_calls == 13                                              # every line voiced once by Gemini TTS

    full, cfg_full = setup.make(only=[])
    assert preview_run(root, cfg_full, load_pricing(root))["cached_shots"] == len(trailer_shots)
    full.run(until="render")                                            # selection changed -> re-render
    assert len(fake.video_calls) == 54                                  # trailer shots were not paid twice
    assert len(fake.tts_calls) == tts_calls                             # TTS cache hit for every line
    wav = next((root / "05_dialogue").glob("*/take_1.wav"))
    assert ffmpeg.duration(wav) == pytest.approx(1.0, abs=0.05)


def test_budget_stops_the_run_and_keeps_paid_work(setup):
    ledger = SpendLedger(setup.root / "reports" / "cost.jsonl", None)
    budget = ledger.spent() + 5.0                       # room for one 8 s standard clip ($3.20), not two
    pipeline, _ = setup.make(budget=budget)
    with pytest.raises(PipelineBlocked, match="budget"):
        pipeline.run(until="render")
    assert ledger.spent() <= budget + 1e-9
    paid_clips = len(list((setup.root / "inbox" / "video").glob("*.mp4")))
    assert paid_clips == len(setup.fake.video_calls) > 0
    resumed, _ = setup.make(budget=500)
    resumed.run(until="render")
    assert len(setup.fake.video_calls) == 54                            # resumed without regenerating


def test_safety_filtered_shot_gets_placeholder_and_review(setup):
    pipeline, _ = setup.make(block_if=lambda prompt: "toy boat" in (prompt or ""))
    pipeline.run(until="render")
    queue = [json.loads(line) for line in (setup.root / "reports" / "review_queue.jsonl").read_text().splitlines()]
    assert [q["shot_id"] for q in queue] == ["sc007_s003"]
    assert (setup.root / "07_video" / "sc007_s003" / "take_1.mp4").exists()


def _line(pace="very_slow", notes="gently, then firm (looks away)"):
    from nraiorbit.schemas import ScriptBlock

    return ScriptBlock.model_validate({
        "block_id": "x", "kind": "dialogue", "character_id": "arjun", "text_te": "భయపడకు. నేనున్నాను కదా.",
        "text_en": "", "delivery": {"rasa": "karuna", "intensity": 6, "pace": pace, "volume": "low", "notes": notes}})


def test_tts_instruction_speaks_only_the_telugu_transcript():
    line = _line()
    voice = {"name": "Arjun (voice artist A)", "gender": "male", "age_range": [28, 38], "pitch": "low"}
    text = tts_instruction(line, voice)
    notes, transcript = text.split("\n#### TRANSCRIPT\n")
    assert transcript == line.text_te                                     # the Telugu line alone, last
    assert text.startswith("# AUDIO PROFILE: Arjun\nmale, age 28-38, low pitch")
    assert "\n### DIRECTOR'S NOTES\n" in notes and "Style: sad, close to tears (karuna" in notes
    assert "Pace: slow and deliberate, but no long gaps" in notes and "Volume: soft" in notes
    assert "Notes: gently, then firm (looks away)" in notes and "looks away" not in transcript
    assert "Speak only the transcript below" in notes and "Do not read these notes aloud" in notes
    assert "slowly" not in text and "Say in" not in text
    for pace, wanted in google_ai.GEMINI_PACE.items():
        assert f"Pace: {wanted}\n" in tts_instruction(_line(pace))
    assert "very slow" not in " ".join(google_ai.GEMINI_PACE.values())
    bare = tts_instruction(_line().model_copy(update={"delivery": None, "character_id": "old_fisherman"}))
    assert bare.startswith("# AUDIO PROFILE: Old Fisherman\n\n") and "Pace: natural conversational pace" in bare


def test_gemini_tts_shortens_pauses_and_keeps_the_paid_take(tmp_path):
    fake = FakeGoogle(tmp_path)
    tone = (np.sin(2 * np.pi * 220 * np.arange(24000) / 24000) * 8000).astype("<i2")

    def gap(s):
        return np.zeros(int(s * 24000), "<i2")

    fake.pcm = np.concatenate([gap(0.4), tone, gap(1.0), tone, gap(0.5)]).tobytes()   # a slow 3.9 s take
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", None)
    voices = {"voices/arjun_voice@v2": {"name": "Arjun (voice artist A)", "gender": "male", "gemini_voice": "Orus"}}
    tts = google_ai.GeminiTTS(voices, {"google": {}, "voice": {"max_pause_s": 0.6}}, {}, ledger, client=fake)
    line = _line()
    seconds = tts.synthesize(line, "voices/arjun_voice@v2", tmp_path / "out" / "take_1.wav")
    assert seconds == pytest.approx(0.06 + 1.0 + 0.3 + 1.0 + 0.06, abs=0.04)
    assert ffmpeg.duration(tmp_path / "out" / "take_1.wav") == pytest.approx(seconds, abs=0.01)
    cached = list((tmp_path / "cache" / "tts").glob("*.wav"))
    assert len(cached) == 1 and ffmpeg.duration(cached[0]) == pytest.approx(3.9, abs=0.02)   # raw take kept
    sent = fake.tts_calls[0]["contents"]
    assert sent.startswith("# AUDIO PROFILE: Arjun") and sent.endswith("#### TRANSCRIPT\n" + line.text_te)
    again = tts.synthesize(line, "voices/arjun_voice@v2", tmp_path / "out" / "take_2.wav")
    assert len(fake.tts_calls) == 1 and again == pytest.approx(seconds, abs=1e-3)          # cache hit, same cut
    assert len([r for r in ledger.records() if r["provider"] == "gemini_tts"]) == 1
    untouched = google_ai.GeminiTTS(voices, {"google": {}, "voice": {"max_pause_s": None}}, {}, ledger, client=fake)
    assert untouched.synthesize(line, "voices/arjun_voice@v2", tmp_path / "out" / "raw.wav") == \
        pytest.approx(3.9, abs=0.02)


def test_gemini_tts_accepts_a_wav_answer(tmp_path):
    """Some TTS models answer with a WAV file instead of raw L16; its header must not become a click."""
    import io
    import wave

    fake = FakeGoogle(tmp_path)
    tone = (np.sin(2 * np.pi * 220 * np.arange(24000) / 24000) * 8000).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(tone.tobytes())
    fake.pcm = buf.getvalue()
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", None)
    tts = google_ai.GeminiTTS({}, {"google": {}, "voice": {"max_pause_s": None}}, {}, ledger, client=fake)
    out = tmp_path / "take_1.wav"
    assert tts.synthesize(_line(), None, out) == pytest.approx(1.0, abs=0.02)            # not 1.0 s + header noise
    with wave.open(str(out)) as w:
        head = np.frombuffer(w.readframes(480), "<i2").astype(float)
    assert np.abs(head[:40]).max() < 3000                                               # no RIFF bytes as samples


def test_trailer_test_run_end_to_end(setup):
    """`nraiorbit run <slug> --only trailer`: movie with placeholders, trailer cut only from generated shots."""
    pipeline, _ = setup.make(only=["trailer"])
    pipeline.run()
    master = setup.root / "11_master"
    assert ffmpeg.duration(master / "movie.mp4") == pytest.approx(300, abs=1.5)
    assert ffmpeg.duration(master / "trailer.mp4") > 10
    generated = {s.shot_id for s in _selected_shots(setup.root, trailer_only=True)}
    plan = json.loads((setup.root / "10_edit" / "trailer_plan.json").read_text())
    used = {c["shot_id"] for c in plan if c["shot_id"]}
    assert used and used <= generated
    assert (master / "subs_te.srt").read_text(encoding="utf-8").count("-->") == 13
    timeline = json.loads((setup.root / "10_edit" / "timeline.json").read_text())
    spans = sorted((e["start_s"], e["start_s"] + e["duration_s"]) for e in timeline["dialogue"])
    assert all(b[0] - a[1] >= 0.25 - 1e-3 for a, b in zip(spans, spans[1:]))     # lines never overlap
    carried = [c for c in plan if c["line_id"]]
    assert carried
    total = sum(c["duration_s"] for c in plan)
    for c in carried:                                                            # every line whole, inside its clip
        assert c["trailer_start_s"] < c["line_at_s"] and c["line_at_s"] + c["line_duration_s"] <= \
            c["trailer_start_s"] + c["duration_s"] + 1e-6 <= total + 1e-6
    notes = json.loads((setup.root / "08_music" / "trailer_cue.json").read_text())["notes"]
    assert "near-silence drop" in notes and "huge hit on the title card" in notes


def test_provider_calls_match_the_real_sdk():
    """The fake client mirrors these signatures; fail loudly if google-genai changes them."""
    import inspect

    from google.genai import types
    from google.genai.files import Files
    from google.genai.models import Models
    from google.genai.operations import Operations

    assert "operation" in inspect.signature(Operations.get).parameters
    assert {"model", "prompt", "image", "config"} <= set(inspect.signature(Models.generate_videos).parameters)
    assert {"model", "contents", "config"} <= set(inspect.signature(Models.generate_content).parameters)
    assert "file" in inspect.signature(Files.download).parameters
    assert {"duration_seconds", "aspect_ratio", "resolution", "negative_prompt", "person_generation",
            "reference_images"} <= set(types.GenerateVideosConfig.model_fields)
    assert {"voice_config", "language_code"} <= set(types.SpeechConfig.model_fields)
    assert "aspect_ratio" in types.ImageConfig.model_fields


def test_retake_generates_only_the_chosen_shot(setup):
    import yaml

    first, _ = setup.make(only=["trailer"])
    first.run()
    calls = len(setup.fake.video_calls)
    (setup.root / "takes.yaml").write_text(yaml.safe_dump({"sc002_s005": 2}))
    again, cfg = setup.make(only=["sc002_s005"])               # what `nraiorbit retake` sets up
    assert preview_run(setup.root, cfg, load_pricing(setup.root))["shots"] == 1
    again.run()
    assert len(setup.fake.video_calls) == calls + 1             # only the retaken shot was paid for
    assert (setup.root / "inbox" / "video" / "sc002_s005_2.mp4").exists()
    timeline = json.loads((setup.root / "10_edit" / "timeline.json").read_text())
    by_id = {s["shot_id"]: s for s in timeline["shots"]}
    assert by_id["sc002_s005"]["clip"].endswith("take_2.mp4")
    trailer_ids = {s.shot_id for s in _selected_shots(setup.root, trailer_only=True)}
    assert all(by_id[sid]["real"] for sid in trailer_ids)        # earlier paid clips still in the film
    plan = json.loads((setup.root / "10_edit" / "trailer_plan.json").read_text())
    assert len({c["shot_id"] for c in plan if c["shot_id"]}) > 3  # trailer still cut from all paid shots


def test_refused_key_type_points_to_vertex(monkeypatch, capsys):
    """New AQ. keys can get 401 ACCESS_TOKEN_TYPE_UNSUPPORTED on the Gemini API; the error must say what to do."""
    from nraiorbit import cli

    class Refused(Exception):
        pass

    Refused.__module__ = "google.genai.errors"

    def refuse():
        raise Refused("401 UNAUTHENTICATED ... ACCESS_TOKEN_TYPE_UNSUPPORTED")

    with pytest.raises(google_ai.GoogleAPIError, match="google.vertex: true"):
        google_ai._call(refuse, "list models")

    def free_tier():
        raise Refused("429 RESOURCE_EXHAUSTED ... generate_content_free_tier_requests, limit: 0")

    with pytest.raises(google_ai.GoogleAPIError, match="free tier.*paid billing account"):
        google_ai._call(free_tier, "Gemini image")

    seen = []
    models = SimpleNamespace(list=lambda: [SimpleNamespace(name="publishers/google/models/veo-3.1-generate-001",
                                                           display_name="Veo 3.1")])
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", lambda cfg: seen.append(cfg) or SimpleNamespace(models=models))
    cli.main(["google", "models", "--vertex"])
    assert seen[-1]["vertex"] is True
    out = capsys.readouterr().out
    assert "Vertex AI" in out and "veo-3.1-generate-001" in out


def test_prepaid_credit_used_up_says_what_to_do():
    class Depleted(Exception):
        pass

    Depleted.__module__ = "google.genai.errors"

    def depleted():
        raise Depleted("402 RESOURCE_EXHAUSTED ... Your prepayment credits are depleted.")

    with pytest.raises(google_ai.GoogleAPIError, match="nothing was charged.*Vertex AI"):
        google_ai._call(depleted, "Gemini image")


def test_vertex_auth_from_service_account_json(monkeypatch):
    from google.oauth2 import service_account

    seen = {}
    monkeypatch.setattr(service_account.Credentials, "from_service_account_info",
                        classmethod(lambda cls, info, scopes: seen.update(info=info, scopes=scopes) or "CREDS"))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON",
                       json.dumps({"type": "service_account", "project_id": "film-proj"}))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    assert google_ai.vertex_auth() == {"credentials": "CREDS", "project": "film-proj", "location": "us-central1"}
    assert seen["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "other-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "asia-south1")
    assert google_ai.vertex_auth()["project"] == "other-proj"
    assert google_ai.vertex_auth()["location"] == "asia-south1"

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "not json")
    with pytest.raises(google_ai.GoogleSetupError, match="not a valid service account key"):
        google_ai.vertex_auth()

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    assert google_ai.vertex_auth() == {"project": "other-proj", "location": "asia-south1"}


def test_vertex_location_per_model(monkeypatch):
    """Veo is regional (us-central1) and the pro image model is served from global: each gets its own client."""
    import sys

    made = []

    class FakeGenaiClient:
        def __init__(self, **kw):
            made.append(kw)

    from google.genai import types

    fake_genai = SimpleNamespace(Client=FakeGenaiClient, types=types)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setattr(sys.modules["google"], "genai", fake_genai, raising=False)
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", None)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "film-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    g = {"vertex": True, "veo_location": "us-central1", "image_location": "global"}

    google_ai.google_client(g, "veo")
    google_ai.google_client(g, "image")
    google_ai.google_client(g, "tts")                      # no tts_location: falls back to GOOGLE_CLOUD_LOCATION
    google_ai.google_client(g)
    assert [m["location"] for m in made] == ["us-central1", "global", "global", "global"]
    assert all(m["vertexai"] is True and m["project"] == "film-proj" for m in made)
    assert [m.get("http_options") is not None for m in made] == [False, True, True, False]  # no auto-retry for Veo
    assert made[1]["http_options"].retry_options.attempts == 5

    made.clear()
    google_ai.google_client({"vertex": False, "veo_location": "us-central1"}, "veo")
    assert made == [{}]                                     # Gemini API: no project/location at all


SHOT = "sc002_s005"                                         # Arjun's market entrance, a trailer shot with a keyframe


def _veo_records(root, shot_id=SHOT):
    return [r for r in SpendLedger(root / "reports" / "cost.jsonl", None).records()
            if r["provider"] == "veo" and r["item"] == shot_id]


def test_interrupted_veo_job_is_collected_not_paid_again(setup):
    """A job that is still running when the run stops (timeout, crash, Ctrl-C) is logged and resumed by name."""
    pipeline, cfg = setup.make(only=[SHOT])
    cfg["google"]["timeout_seconds"] = -1                   # stop right after the job is submitted
    with pytest.raises(PipelineBlocked, match="already paid for"):
        pipeline.run(until="render")
    pending = setup.root / "inbox" / "video" / ".pending" / f"{SHOT}.json"
    assert pending.exists() and len(setup.fake.video_calls) == 1
    assert sum(r["usd"] for r in _veo_records(setup.root)) == pytest.approx(3.20)   # counts toward the budget

    resumed, _ = setup.make(only=[SHOT])
    resumed.run(until="render")
    assert len(setup.fake.video_calls) == 1                  # collected, not submitted again
    assert sum(r["usd"] for r in _veo_records(setup.root)) == pytest.approx(3.20)   # and not logged twice
    assert (setup.root / "inbox" / "video" / f"{SHOT}.mp4").exists() and not pending.exists()


def test_veo_polling_rides_out_a_server_hiccup(setup):
    setup.fake.poll_failures = 2
    pipeline, _ = setup.make(only=[SHOT])
    pipeline.run(until="render")
    assert len(setup.fake.video_calls) == 1 and (setup.root / "inbox" / "video" / f"{SHOT}.mp4").exists()
    assert setup.fake.poll_configs[-1].http_options.timeout == 120_000      # a dead connection can't hang the run


def test_network_error_stops_cleanly():
    import httpx

    def drop():
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    with pytest.raises(google_ai.GoogleAPIError, match="network error.*Re-run to resume"):
        google_ai._call(drop, "Veo sc002_s005")


def test_clip_on_disk_is_reused_by_attempt_number(setup):
    """A take whose number skips one (e.g. take 3 after a failed take 2) is still never paid for twice."""
    from nraiorbit.providers.base import RenderContext

    pipeline, cfg = setup.make(only=[SHOT])
    pipeline.run(until="render")
    inbox = setup.root / "inbox" / "video"
    shutil.copy(inbox / f"{SHOT}.mp4", inbox / f"{SHOT}_3.mp4")            # attempt 3 exists, attempt 2 doesn't
    video = google_ai.VeoVideo(setup.root, setup.library, cfg, load_pricing(setup.root), pipeline.ledger, {},
                               client=setup.fake)
    calls = len(setup.fake.video_calls)
    shot = next(s for s in _selected_shots(setup.root, trailer_only=False) if s.shot_id == SHOT)
    ctx = RenderContext(scene_id="sc002", cast=pipeline._lock(), resolution=(320, 180), fps=24, seed=3)
    video.render_shot(shot, setup.root / "take_3.mp4", ctx)
    assert len(setup.fake.video_calls) == calls


def test_veo_operation_errors(setup):
    """A policy refusal becomes a placeholder + review queue entry; other errors stop cleanly. Neither is billed."""
    setup.fake.op_error = {"code": 3, "message": "The prompt could not be submitted: it violates usage guidelines."}
    pipeline, _ = setup.make(only=[SHOT])
    pipeline.run(until="render")
    queue = [json.loads(line) for line in (setup.root / "reports" / "review_queue.jsonl").read_text().splitlines()]
    assert [q["shot_id"] for q in queue] == [SHOT]
    assert sum(r["usd"] for r in _veo_records(setup.root)) == pytest.approx(0.0)

    setup.fake.op_error = {"code": 13, "message": "Internal error."}
    other = "sc009_s004"
    pipeline, _ = setup.make(only=[other])
    with pytest.raises(PipelineBlocked, match="Veo sc009_s004 failed: Internal error"):
        pipeline.run(until="render")
    assert sum(r["usd"] for r in _veo_records(setup.root, other)) == pytest.approx(0.0)
    assert not list((setup.root / "inbox" / "video" / ".pending").glob("*.json"))


def test_vertex_veo_turns_off_unused_audio(setup):
    pipeline, cfg = setup.make(only=[SHOT])
    cfg["google"]["vertex"] = True
    pipeline.run(until="render")
    config = setup.fake.video_calls[-1]["config"]
    assert config.generate_audio is False and config.number_of_videos == 1


def test_lip_sync_redraws_only_on_screen_speakers_with_their_own_lines(setup):
    from nraiorbit.providers.mock import MockLipSync
    from nraiorbit.schemas import SceneScript

    MockLipSync.calls.clear()
    pipeline, cfg = setup.make(only=["trailer"])
    cfg["providers"]["lipsync"] = "mock"
    pipeline.run()
    timeline = json.loads((setup.root / "10_edit" / "timeline.json").read_text())
    synced = {s["shot_id"] for s in timeline["shots"] if s["clip"].endswith("_lipsync.mp4")}
    speaker = {b.block_id: b.character_id for p in (setup.root / "03_scenes").glob("*.json")
               for b in SceneScript.model_validate_json(p.read_text()).blocks if b.kind == "dialogue"}
    real = {s["shot_id"] for s in timeline["shots"] if s["real"]}
    shots = {s.shot_id: s for s in _selected_shots(setup.root, trailer_only=False)}
    expected = {sid for sid, s in shots.items() if sid in real
                and any(speaker.get(b) in s.characters_in_frame for b in s.block_ids)}
    assert synced == expected and synced and len(MockLipSync.calls) == len(synced)
    for video, audio in MockLipSync.calls:                       # the audio is exactly the shot's length
        sid = video.parent.name
        assert ffmpeg.duration(audio) == pytest.approx(shots[sid].duration_s, abs=0.02)
    for s in timeline["shots"]:
        if s["shot_id"] in synced:                               # conformed back for stream-copy joins
            assert ffmpeg.duration(s["clip"]) == pytest.approx(s["duration_s"], abs=0.05)
    assert ffmpeg.duration(setup.root / "11_master" / "trailer.mp4") > 10


def _set_tier(root, shot_id, tier):
    for path in (root / "04_shots").glob("*.json"):
        shot_list = ShotList.model_validate_json(path.read_text())
        for s in shot_list.shots:
            if s.shot_id == shot_id:
                s.video_tier = tier
                path.write_text(shot_list.model_dump_json(indent=2))
                return s
    raise KeyError(shot_id)


def test_shot_video_tier_picks_model_resolution_length_and_price(setup):
    from nraiorbit.costing import veo_clip_seconds

    short = "sc003_s004"                                     # a 5 s shot of Arjun
    shot = _set_tier(setup.root, short, "fast_720p")
    seconds = veo_clip_seconds(shot.duration_s, load_pricing(setup.root), "720p", "keyframe", True)
    assert seconds == 6                                      # 720p allows a 6 s clip; 1080p would be 8 s
    pipeline, cfg = setup.make(only=[short])
    cfg["google"]["vertex"] = True                           # Vertex, Veo audio off: video-only price
    assert preview_run(setup.root, cfg, load_pricing(setup.root))["video_usd"] == pytest.approx(seconds * 0.08)
    pipeline.run(until="render")
    call = setup.fake.video_calls[-1]
    assert call["model"] == "veo-3.1-fast-generate-001"
    assert call["config"].resolution == "720p" and call["config"].duration_seconds == seconds
    (rec,) = _veo_records(setup.root, short)
    assert rec["usd"] == pytest.approx(seconds * 0.08) and rec["seconds"] == seconds
    assert (rec["tier"], rec["resolution"], rec["model"]) == ("fast", "720p", "veo-3.1-fast-generate-001")


def test_budget_counts_the_first_frame_with_its_clip(setup):
    pipeline, cfg = setup.make(only=[SHOT])
    pre = preview_run(setup.root, cfg, load_pricing(setup.root))
    assert pre["keyframes"] == 1
    images = len(setup.fake.image_calls)
    spent = SpendLedger(setup.root / "reports" / "cost.jsonl", None).spent()
    room = spent + pre["video_usd"] + pre["keyframe_usd"] / 2         # the clip fits, the clip and its still do not
    pipeline, _ = setup.make(budget=room, only=[SHOT])
    with pytest.raises(PipelineBlocked, match="budget"):
        pipeline.run(until="render")
    assert len(setup.fake.image_calls) == images and setup.fake.video_calls == []
    assert _veo_records(setup.root) == []


def test_unknown_video_tier_stops_before_anything_is_paid(setup):
    _set_tier(setup.root, SHOT, "turbo_4k")
    images, videos = len(setup.fake.image_calls), len(setup.fake.video_calls)
    pipeline, _ = setup.make(only=[SHOT])
    with pytest.raises(PipelineBlocked, match="video_tier 'turbo_4k' is not in google.video_tiers"):
        pipeline.run(until="render")
    assert (len(setup.fake.image_calls), len(setup.fake.video_calls)) == (images, videos)
    assert _veo_records(setup.root) == []


def test_resumed_job_keeps_the_cost_it_was_submitted_with(setup):
    _set_tier(setup.root, SHOT, "lite_720p")
    pipeline, cfg = setup.make(only=[SHOT])
    cfg["google"]["timeout_seconds"] = -1
    with pytest.raises(PipelineBlocked, match="already paid for"):
        pipeline.run(until="render")
    pending = setup.root / "inbox" / "video" / ".pending" / f"{SHOT}.json"
    job = json.loads(pending.read_text())
    assert (job["tier"], job["resolution"], job["model"]) == ("lite", "720p", "veo-3.1-lite-generate-001")
    paid = sum(r["usd"] for r in _veo_records(setup.root))
    assert paid == pytest.approx(job["seconds"] * 0.05)

    _set_tier(setup.root, SHOT, "standard_1080p")           # the shot's tier changes before the job is collected
    resumed, _ = setup.make(only=[SHOT])
    resumed.run(until="render")
    assert len(setup.fake.video_calls) == 1 and not pending.exists()
    assert sum(r["usd"] for r in _veo_records(setup.root)) == pytest.approx(paid)


def _cli_for(setup, monkeypatch):
    from nraiorbit import cli, llm

    monkeypatch.setattr(cli, "PROJECTS", setup.root.parent)
    monkeypatch.setattr(llm, "ClaudeClient", lambda **kw: None)
    monkeypatch.setattr(cli, "Pipeline", lambda root, llm_, cfg: Pipeline(
        root, llm=None, library=setup.library, log=lambda m: None,
        cfg=deep_merge(cfg, {"film": {"resolution": [320, 180]}, "google": {"poll_seconds": 0}})))
    monkeypatch.setattr(cli, "_run", lambda p, slug, until=None, force=(): p.run(until="render"))
    return cli


def test_retake_keep_keyframe_reuses_the_first_frame(setup, monkeypatch, capsys):
    pipeline, _ = setup.make(only=[SHOT])
    pipeline.run(until="render")
    frames = setup.root / "06_keyframes" / SHOT
    assert [p.name for p in frames.glob("take_*.png")] == ["take_1.png"]
    images, videos = len(setup.fake.image_calls), len(setup.fake.video_calls)

    cli = _cli_for(setup, monkeypatch)
    cli.main(["retake", setup.root.name, SHOT, "--keep-keyframe", "--yes"])
    out = capsys.readouterr().out
    assert "first frame from take_1.png" in out and "Keyframes: 0 images" in out
    assert (frames / "take_2.png").read_bytes() == (frames / "take_1.png").read_bytes()
    assert len(setup.fake.image_calls) == images                          # no new image paid for
    assert len(setup.fake.video_calls) == videos + 1
    assert setup.fake.video_calls[-1]["image"] is not None
    assert (setup.root / "inbox" / "video" / f"{SHOT}_2.mp4").exists()

    with pytest.raises(SystemExit, match="--keep-keyframe is for a new attempt"):
        cli.main(["retake", setup.root.name, SHOT, "--keep-keyframe", "--use", "1"])
    textless = next(s.shot_id for s in _selected_shots(setup.root, trailer_only=False) if not s.characters_in_frame)
    (setup.root / "inbox" / "video" / f"{textless}.mp4").write_bytes(b"")     # pretend it was generated
    with pytest.raises(SystemExit, match="no keyframe to keep"):
        cli.main(["retake", setup.root.name, textless, "--keep-keyframe", "--yes"])
    assert len(setup.fake.video_calls) == videos + 1


def test_retake_keep_keyframe_uses_the_take_in_use_and_queues_nothing_unconfirmed(setup, monkeypatch):
    import shutil

    import yaml

    pipeline, _ = setup.make(only=[SHOT])
    pipeline.run(until="render")
    frames, inbox = setup.root / "06_keyframes" / SHOT, setup.root / "inbox" / "video"
    (frames / "take_2.png").write_bytes(b"rejected still")              # attempt 2 had the wrong still
    shutil.copy2(inbox / f"{SHOT}.mp4", inbox / f"{SHOT}_2.mp4")
    cli = _cli_for(setup, monkeypatch)
    cli.main(["retake", setup.root.name, SHOT, "--use", "1", "--yes"])
    takes = setup.root / "takes.yaml"
    before = takes.read_text()
    new = frames / "take_3.png"

    monkeypatch.setattr(cli, "_confirm", lambda question, assume_yes: assume_yes)   # without --yes: declined
    cli.main(["retake", setup.root.name, SHOT, "--keep-keyframe"])     # declined: nothing left behind
    assert not new.exists() and takes.read_text() == before
    new.write_bytes(b"mine")                                           # an existing take file is never touched
    cli.main(["retake", setup.root.name, SHOT, "--keep-keyframe"])
    assert new.read_bytes() == b"mine" and takes.read_text() == before
    new.unlink()

    _set_tier(setup.root, SHOT, "turbo_4k")                            # the spend preview fails: undone as well
    with pytest.raises(SystemExit, match="video_tier 'turbo_4k'"):
        cli.main(["retake", setup.root.name, SHOT, "--keep-keyframe", "--yes"])
    assert not new.exists() and takes.read_text() == before

    _set_tier(setup.root, SHOT, None)
    videos = len(setup.fake.video_calls)
    cli.main(["retake", setup.root.name, SHOT, "--keep-keyframe", "--yes"])
    assert new.read_bytes() == (frames / "take_1.png").read_bytes()    # the still of the attempt in use
    assert yaml.safe_load(takes.read_text())[SHOT] == 3 and len(setup.fake.video_calls) == videos + 1
