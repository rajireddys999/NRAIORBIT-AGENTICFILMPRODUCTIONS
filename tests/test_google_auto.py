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

    def make(budget=50.0, only=None, block_if=None):
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
    assert cfg0.aspect_ratio == "16:9" and cfg0.resolution == "720p" and cfg0.duration_seconds in (4, 6, 8)
    assert "Arjun" in next(c["prompt"] for c in fake.video_calls if "Arjun" in c["prompt"])

    ledger = SpendLedger(root / "reports" / "cost.jsonl", None)
    veo_seconds = sum(r.get("seconds", 0) for r in ledger.records() if r["provider"] == "veo")
    assert ledger.summary()["veo"] == pytest.approx(veo_seconds * 0.10)
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
    pipeline, _ = setup.make(budget=1.25)
    with pytest.raises(PipelineBlocked, match="budget"):
        pipeline.run(until="render")
    ledger = SpendLedger(setup.root / "reports" / "cost.jsonl", None)
    assert ledger.spent() <= 1.25 + 1e-9
    paid_clips = len(list((setup.root / "inbox" / "video").glob("*.mp4")))
    assert paid_clips == len(setup.fake.video_calls) > 0
    resumed, _ = setup.make(budget=100)
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
