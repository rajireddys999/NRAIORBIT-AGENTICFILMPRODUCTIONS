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
        self.video_calls, self.image_calls, self.tts_calls = [], [], []
        self.models = SimpleNamespace(generate_videos=self._videos, generate_content=self._content, list=lambda: [])
        self.operations = SimpleNamespace(get=self._get)
        self.files = SimpleNamespace(download=lambda file: None)

    def _videos(self, *, model, prompt=None, image=None, config=None):
        self.video_calls.append({"model": model, "prompt": prompt, "image": image, "config": config})
        return SimpleNamespace(done=False, error=None, response=None, result=None, blocked=self.block_if(prompt))

    def _get(self, operation):           # same keyword as google.genai Operations.get
        op = operation
        clip = self.clip
        videos = [] if op.blocked else [SimpleNamespace(video=SimpleNamespace(save=lambda p: shutil.copy(clip, p)))]
        return SimpleNamespace(done=True, error=None, result=None, blocked=op.blocked, response=SimpleNamespace(
            generated_videos=videos, rai_media_filtered_reasons=["test filter"] if op.blocked else None))

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
    shutil.copytree(REPO_ROOT / "library", lib_root)          # identity images must not land in the repo
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


def test_tts_instruction_is_telugu_with_delivery():
    from nraiorbit.schemas import ScriptBlock

    line = ScriptBlock.model_validate({
        "block_id": "x", "kind": "dialogue", "character_id": "arjun", "text_te": "భయపడకు.", "text_en": "",
        "delivery": {"rasa": "karuna", "intensity": 6, "pace": "slow", "volume": "low", "notes": "gently"}})
    text = tts_instruction(line)
    assert text.startswith("Say in natural spoken Telugu") and "slowly" in text and text.endswith("భయపడకు.")


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

    seen = []
    models = SimpleNamespace(list=lambda: [SimpleNamespace(name="publishers/google/models/veo-3.1-generate-001",
                                                           display_name="Veo 3.1")])
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", lambda cfg: seen.append(cfg) or SimpleNamespace(models=models))
    cli.main(["google", "models", "--vertex"])
    assert seen[-1]["vertex"] is True
    out = capsys.readouterr().out
    assert "Vertex AI" in out and "veo-3.1-generate-001" in out
