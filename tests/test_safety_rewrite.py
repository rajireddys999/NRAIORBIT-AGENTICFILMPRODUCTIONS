"""A first frame Google refuses for safety is not left for the owner to fix by hand: the director words the shot again
once (the same moment, without what the filter blocks), the shot list is saved with it, the frame is drawn from it,
and the clip is asked for in the same words. A second refusal stands and goes to the review queue."""

import json
from types import SimpleNamespace

import pytest

from nraiorbit.config import load_navarasa
from nraiorbit.demo import OUTLINE, SHOTS, STORY_BIBLE
from nraiorbit.pipeline import SAFETY_REWRITES, safer_shot_fn
from nraiorbit.schemas import CastLock, Outline, SaferShot, ShotList, StoryBible


@pytest.fixture
def root(tmp_path):
    (tmp_path / "01_story_bible.json").write_text(json.dumps(STORY_BIBLE), encoding="utf-8")
    (tmp_path / "02_outline.json").write_text(Outline.model_validate(OUTLINE).model_dump_json(), encoding="utf-8")
    (tmp_path / "04_shots").mkdir()
    (tmp_path / "04_shots" / "sc001.json").write_text(
        ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).model_dump_json(), encoding="utf-8")
    return tmp_path


def _shot(root):
    return ShotList.model_validate_json((root / "04_shots" / "sc001.json").read_text(encoding="utf-8")).shots[0]


class Director:
    def __init__(self, fail=False):
        self.asked, self.fail = [], fail

    def safer_shot(self, bible, shot, refusal):
        self.asked.append((shot.shot_id, refusal))
        if self.fail:
            raise RuntimeError("model unavailable")
        return SaferShot(image_prompt="a calm wide frame of the cliff at dusk", motion_prompt="the camera drifts up")


def test_a_refused_shot_is_worded_again_once_and_saved_with_both_wordings(root):
    director, logs = Director(), []
    safer = safer_shot_fn(root, lambda: director, logs.append)
    shot = _shot(root)
    fixed = safer(shot, "sc001", "no media returned (finish_reason=IMAGE_SAFETY)")
    assert (fixed.image_prompt, fixed.motion_prompt) == ("a calm wide frame of the cliff at dusk", "the camera drifts up")
    assert fixed.model_dump(exclude={"image_prompt", "motion_prompt"}) == \
        shot.model_dump(exclude={"image_prompt", "motion_prompt"})               # size, cast, length: all kept
    saved = ShotList.model_validate_json((root / "04_shots" / "sc001.json").read_text(encoding="utf-8"))
    assert saved.shots[0] == fixed and saved.shots[1:] == ShotList.model_validate(
        {"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[1:]
    rec = json.loads((root / "reports" / SAFETY_REWRITES).read_text(encoding="utf-8"))
    assert rec["shot_id"] == shot.shot_id and rec["before"]["image_prompt"] == shot.image_prompt
    assert rec["after"]["image_prompt"] == fixed.image_prompt and "IMAGE_SAFETY" in rec["refusal"]
    assert safer(fixed, "sc001", "refused again") is None                        # once per shot, never a third take
    assert len(director.asked) == 1 and "refused again after it was worded again" in logs[-1]


def test_a_rewrite_that_fails_leaves_the_refusal_standing(root):
    logs = []
    assert safer_shot_fn(root, lambda: Director(fail=True), logs.append)(_shot(root), "sc001", "IMAGE_SAFETY") is None
    assert "could not be worded again (RuntimeError: model unavailable)" in logs[0]
    assert not (root / "reports" / SAFETY_REWRITES).exists()


def _veo(root):
    from nraiorbit.costing import SpendLedger, load_pricing
    from nraiorbit.providers.google_ai import VeoVideo
    return VeoVideo(root, None, {"google": {}}, load_pricing(None), SpendLedger(root / "cost.jsonl", None),
                    load_navarasa(), client=object())


def test_the_first_frame_is_drawn_from_the_safer_words_after_a_refusal(root):
    from nraiorbit.providers.google_ai import ShotFiltered
    prompts = []

    def generate(prompt, out, *, refs=(), item=""):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ShotFiltered("no media returned (finish_reason=FinishReason.IMAGE_SAFETY)")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"png")
        return out
    video = _veo(root)
    video.images = SimpleNamespace(generate=generate, price=0.04)
    video.safer = safer_shot_fn(root, lambda: Director(), lambda *_: None)
    shot = _shot(root).model_copy(update={"characters_in_frame": [], "costume_ids": []})
    keyframe = root / "06_keyframes" / shot.shot_id / "take_1.png"
    drawn = video._first_frame(shot, "sc001", CastLock(characters={}), [], keyframe)
    assert keyframe.exists() and drawn.image_prompt == "a calm wide frame of the cliff at dusk"
    assert shot.image_prompt.rstrip(".") in prompts[0] and "A calm wide frame of the cliff at dusk" not in prompts[0]
    assert "a calm wide frame of the cliff at dusk" in prompts[1] and shot.image_prompt.rstrip(".") not in prompts[1]


def test_without_a_rewriter_or_after_a_second_refusal_the_refusal_is_raised(root):
    from nraiorbit.providers.google_ai import ShotFiltered

    def refuse(prompt, out, *, refs=(), item=""):
        raise ShotFiltered("IMAGE_SAFETY")
    video = _veo(root)
    video.images = SimpleNamespace(generate=refuse, price=0.04)
    shot = _shot(root).model_copy(update={"characters_in_frame": [], "costume_ids": []})
    keyframe = root / "06_keyframes" / shot.shot_id / "take_1.png"
    with pytest.raises(ShotFiltered):
        video._first_frame(shot, "sc001", CastLock(characters={}), [], keyframe)    # no rewriter: as before
    director = Director()
    video.safer = safer_shot_fn(root, lambda: director, lambda *_: None)
    with pytest.raises(ShotFiltered):
        video._first_frame(shot, "sc001", CastLock(characters={}), [], keyframe)    # worded again, refused again
    assert len(director.asked) == 1 and not keyframe.exists()
    assert StoryBible.model_validate(STORY_BIBLE)                                    # the fixture's bible is valid


def test_the_clip_is_asked_for_in_the_words_its_first_frame_was_drawn_from(root):
    """render_shot: the first frame is refused, worded again and drawn; Veo then gets the new words too, so the clip
    does not walk into the same refusal (Veo answers with a refusal here, which is enough to read what it was sent)."""
    pytest.importorskip("google.genai")
    from nraiorbit.providers import RenderContext
    from nraiorbit.providers.google_ai import ShotFiltered
    sent, prompts = [], []

    def generate(prompt, out, *, refs=(), item=""):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ShotFiltered("no media returned (finish_reason=FinishReason.IMAGE_SAFETY)")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        return out

    class Models:
        def generate_videos(self, **kw):
            sent.append(kw)
            return SimpleNamespace(name=None, done=True, error={"code": 3, "message": "guideline"})
    video = _veo(root)
    video.client = SimpleNamespace(models=Models())
    video.images = SimpleNamespace(generate=generate, price=0.04)
    video.safer = safer_shot_fn(root, lambda: Director(), lambda *_: None)
    ref = root / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    video._refs = lambda shot, lock, limit: [(c, [ref]) for c in shot.characters_in_frame]
    shot = next(s for s in ShotList.model_validate_json((root / "04_shots" / "sc001.json").read_text(
        encoding="utf-8")).shots if s.characters_in_frame)                         # a shot with a lead in it
    with pytest.raises(ShotFiltered):
        video.render_shot(shot, root / "out.mp4", RenderContext(cast=CastLock(characters={}), scene_id="sc001",
                                                                resolution=(1280, 720), fps=24, seed=1))
    assert len(sent) == 1 and "a calm wide frame of the cliff at dusk" in sent[0]["prompt"]
    assert "The camera drifts up" in sent[0]["prompt"] and shot.image_prompt.rstrip(".") not in sent[0]["prompt"]


def test_only_the_words_change_in_the_shot_list_when_the_shot_in_hand_is_trimmed_for_the_edit(root):
    shot = _shot(root)
    trimmed = shot.model_copy(update={"duration_s": shot.duration_s - 2})            # as the edit hands it over
    fixed = safer_shot_fn(root, lambda: Director(), lambda *_: None)(trimmed, "sc001", "IMAGE_SAFETY")
    assert fixed.duration_s == trimmed.duration_s                                     # the render goes on trimmed
    saved = ShotList.model_validate_json((root / "04_shots" / "sc001.json").read_text(encoding="utf-8")).shots[0]
    assert saved.duration_s == shot.duration_s and saved.image_prompt == fixed.image_prompt



def _render_setup(root, generate, refuse_veo=True):
    from nraiorbit.providers import RenderContext
    sent = []

    class Models:
        def generate_videos(self, **kw):
            sent.append(kw)
            return SimpleNamespace(name=None, done=True, error={"code": 3, "message": "guideline: knife"})
    video = _veo(root)
    video.client = SimpleNamespace(models=Models())
    video.images = SimpleNamespace(generate=generate, price=0.04)
    video.log = lambda *_: None
    ref = root / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    video._refs = lambda shot, lock, limit: [(c, [ref]) for c in shot.characters_in_frame]
    shot = next(s for s in ShotList.model_validate_json((root / "04_shots" / "sc001.json").read_text(
        encoding="utf-8")).shots if s.characters_in_frame)
    ctx = RenderContext(cast=CastLock(characters={}), scene_id="sc001", resolution=(1280, 720), fps=24, seed=1)
    return video, sent, shot, ctx


def test_a_clip_veo_refuses_gets_new_words_and_a_new_first_frame_once(root):
    """Veo refuses the clip (often for what is in the picture): the shot is worded again, its first frame is set aside
    and drawn again from the new words, and Veo is asked once more with both. A second refusal stands."""
    pytest.importorskip("google.genai")
    from nraiorbit.providers.google_ai import ShotFiltered
    drawn = []

    def generate(prompt, out, *, refs=(), item=""):
        drawn.append(prompt)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        return out
    video, sent, shot, ctx = _render_setup(root, generate)
    director = Director()
    video.safer = safer_shot_fn(root, lambda: director, lambda *_: None)
    with pytest.raises(ShotFiltered):
        video.render_shot(shot, root / "out.mp4", ctx)
    assert len(director.asked) == 1 and "guideline: knife" in director.asked[0][1]
    assert len(drawn) == 2 and "a calm wide frame of the cliff at dusk" in drawn[1]   # the frame drawn again
    assert (root / "06_keyframes" / shot.shot_id / "take_1.refused.png").exists()
    assert len(sent) == 2 and shot.image_prompt.rstrip(".").lower() in sent[0]["prompt"].lower()
    assert "a calm wide frame of the cliff at dusk" in sent[1]["prompt"].lower()
    assert video._words(shot).image_prompt == "a calm wide frame of the cliff at dusk"   # a later take: new words


def test_a_veo_clip_is_not_sent_when_a_refused_frame_and_its_redraw_used_up_the_cap(root):
    """The frame refused and its redraw are both on the ledger before Veo is asked, so the cap holds."""
    pytest.importorskip("google.genai")
    from nraiorbit.costing import BudgetExceeded
    from nraiorbit.providers.google_ai import ShotFiltered

    def generate(prompt, out, *, refs=(), item=""):
        video.ledger.record("gemini", item, 0.04)
        if len(video.ledger.records()) == 1:
            raise ShotFiltered("no media returned (finish_reason=FinishReason.IMAGE_SAFETY)")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        return out
    video, sent, shot, ctx = _render_setup(root, generate)
    video.safer = safer_shot_fn(root, lambda: Director(), lambda *_: None)
    from nraiorbit.costing import SpendLedger, veo_clip_seconds, veo_price_per_second
    model, tier, resolution = video._settings(shot)
    veo = veo_clip_seconds(shot.duration_s, video.pricing, resolution, "keyframe", True) * \
        veo_price_per_second(video.pricing, video.g, tier, resolution)
    video.ledger = SpendLedger(root / "capped.jsonl", round(veo + 0.05, 4))       # Veo and one frame fit, not two
    with pytest.raises(BudgetExceeded):
        video.render_shot(shot, root / "out.mp4", ctx)
    assert sent == [] and len(video.ledger.records()) == 2                          # two frames, no clip


def test_a_re_planned_shot_can_be_worded_again_and_a_cut_short_record_line_is_skipped(root):
    director, logs = Director(), []
    safer = safer_shot_fn(root, lambda: director, logs.append)
    (root / "reports").mkdir()
    (root / "reports" / SAFETY_REWRITES).write_text('{"shot_id": "sc001_s001", "after": {"image_prompt": "x"', "utf-8")
    shot = _shot(root)
    fixed = safer(shot, "sc001", "IMAGE_SAFETY")                                    # the broken line is skipped
    assert fixed is not None and safer(fixed, "sc001", "again") is None             # its own rewrite: not twice
    replanned = shot.model_copy(update={"image_prompt": "a new plan for this shot id"})
    assert safer(replanned, "sc001", "IMAGE_SAFETY") is not None and len(director.asked) == 2



def test_a_retired_preview_video_model_is_replaced_by_its_released_name_once(root):
    """Google retired veo-3.1-fast-generate-preview on Vertex AI (404 NOT_FOUND: the live film stopped there). The
    runner asks again with the released name, which nothing was paid for before, and keeps it for the next shots."""
    pytest.importorskip("google.genai")
    from nraiorbit.providers.google_ai import ShotFiltered, other_name
    assert other_name("veo-3.1-fast-generate-preview") == "veo-3.1-fast-generate-001"
    assert other_name("veo-3.1-generate-001") == "veo-3.1-generate-preview" and other_name("lyria-002") is None

    class NotFound(Exception):
        __module__ = "google.genai.errors"

    def generate(prompt, out, *, refs=(), item=""):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        return out
    video, sent, shot, ctx = _render_setup(root, generate)
    asked, logs = [], []

    class Models:
        def generate_videos(self, **kw):
            asked.append(kw["model"])
            if kw["model"].endswith("-preview"):
                raise NotFound("404 NOT_FOUND. Publisher model `veo-3.1-fast-generate-preview` was not found")
            return SimpleNamespace(name=None, done=True, error={"code": 3, "message": "guideline"})
    video.client, video.log = SimpleNamespace(models=Models()), logs.append
    video.g = {**video.g, "veo_model": "veo-3.1-fast-generate-preview", "veo_tier": "fast"}
    for take in (1, 2):                                                     # two shots' worth of calls
        with pytest.raises(ShotFiltered):
            video.render_shot(shot, root / "out.mp4", type(ctx)(**{**ctx.__dict__, "seed": take}))
    assert asked == ["veo-3.1-fast-generate-preview", "veo-3.1-fast-generate-001", "veo-3.1-fast-generate-001"]
    assert "Google does not offer veo-3.1-fast-generate-preview here" in logs[0]
    assert abs(sum(r.get("usd", 0) for r in video.ledger.records() if r.get("provider") == "veo")) < 1e-9  # refunded


def test_with_the_faces_gate_on_a_redrawn_still_waits_for_a_look_before_any_clip_is_paid(root):
    """gates: ask. Veo refuses an approved shot: the still is redrawn from the new words but not animated; the
    pipeline then holds faces again (faces.pending, the approval taken back), and a Retry cannot skip that hold."""
    pytest.importorskip("google.genai")
    from nraiorbit.providers.google_ai import FirstFrameToLookAt

    def generate(prompt, out, *, refs=(), item=""):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        return out
    video, sent, shot, ctx = _render_setup(root, generate)
    video.safer, video.look_first = safer_shot_fn(root, lambda: Director(), lambda *_: None), True
    keyframe = root / "06_keyframes" / shot.shot_id / "take_1.png"
    generate("approved", keyframe)                                                  # the still the owner approved
    with pytest.raises(FirstFrameToLookAt):
        video.render_shot(shot, root / "out.mp4", ctx)
    assert len(sent) == 1 and keyframe.exists() and keyframe.with_name("take_1.refused.png").exists()

    from nraiorbit.pipeline import Pipeline
    from nraiorbit.service.produce import _faces_needed
    p = SimpleNamespace(p=SimpleNamespace(video=root / "07_video", reviews=root / "reviews", root=root),
                        cfg={"retakes": {"max_per_shot": 1}}, takes={}, _to_look=[], log=lambda *_: None,
                        _report=lambda *a: None)
    clip = Pipeline._render_shot(p, SimpleNamespace(render_shot=lambda *a: (_ for _ in ()).throw(
        FirstFrameToLookAt(shot.shot_id))), shot, ctx, None)
    assert p._to_look == [shot.shot_id] and not clip.exists()                     # nothing placed, nothing paid
    (root / "inbox" / "video" / shot.shot_id).mkdir(parents=True)
    (root / "inbox" / "video" / shot.shot_id / "x.mp4").write_bytes(b"")         # clips exist already
    films = SimpleNamespace(p=SimpleNamespace(reviews=root / "reviews", root=root),
                            cfg={"providers": {"video": "veo"}, "google": {"veo_mode": "keyframe"}})
    assert not _faces_needed(films)
    (root / "reviews").mkdir(exist_ok=True)
    (root / "reviews" / "faces.pending").write_text("look", encoding="utf-8")
    assert _faces_needed(films)                                                     # held: a Retry waits too
    (root / "reviews" / "faces.approved").write_text("", encoding="utf-8")
    assert not _faces_needed(films)                                                 # approved: the clip is made


def test_a_failed_rewrite_of_a_refused_still_is_asked_for_once(root):
    from nraiorbit.providers.google_ai import ShotFiltered
    calls = []

    def refuse(prompt, out, *, refs=(), item=""):
        raise ShotFiltered("IMAGE_SAFETY")
    video = _veo(root)
    video.images = SimpleNamespace(generate=refuse, price=0.04)
    video.safer = lambda shot, scene_id, refusal: calls.append(shot.shot_id)       # the rewrite fails: None
    shot = _shot(root).model_copy(update={"characters_in_frame": [], "costume_ids": []})
    keyframe = root / "06_keyframes" / shot.shot_id / "take_1.png"
    for _ in range(2):
        with pytest.raises(ShotFiltered):
            video._first_frame(shot, "sc001", CastLock(characters={}), [], keyframe)
    assert calls == [shot.shot_id]


def test_the_run_stops_at_faces_naming_the_new_stills_and_takes_the_approval_back(root):
    from nraiorbit.pipeline import GatePending, Pipeline
    reviews = root / "reviews"
    reviews.mkdir()
    (reviews / "faces.approved").write_text("", encoding="utf-8")
    p = SimpleNamespace(p=SimpleNamespace(reviews=reviews, root=root), _to_look=[])
    Pipeline._hold_for_new_frames(p)                                                # nothing new: the run goes on
    assert (reviews / "faces.approved").exists()
    p._to_look = ["sc008_s002", "sc012_s002"]
    with pytest.raises(GatePending) as held:
        Pipeline._hold_for_new_frames(p)
    assert held.value.gate == "faces" and not (reviews / "faces.approved").exists()
    assert "sc008_s002, sc012_s002" in (reviews / "faces.pending").read_text(encoding="utf-8")

