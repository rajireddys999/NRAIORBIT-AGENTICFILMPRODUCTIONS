import pytest

from nraiorbit.config import load_navarasa
from nraiorbit.demo import CUE_REVISIONS, OUTLINE, SHOTS, STORY_BIBLE
from nraiorbit.planning.music import apply_revisions, plan_cues
from nraiorbit.planning.runtime import (
    fit_shots_to_scene, rebalance_outline, scene_start_times, template_blocks, validate_outline,
)
from nraiorbit.schemas import CueRevisions, Outline, ShotList, StoryBible


@pytest.fixture
def outline():
    return Outline.model_validate(OUTLINE)


@pytest.fixture
def bible():
    return StoryBible.model_validate(STORY_BIBLE)


def test_template_covers_feature_runtime():
    blocks = template_blocks("telugu_commercial", 105)
    assert blocks[0].start_s == 0
    assert blocks[-1].end_s == 105 * 60
    interval = next(b for b in blocks if b.name == "interval_bang")
    assert 54 <= interval.start_s / 60 <= 56


def test_rebalance_hits_runtime_exactly(outline):
    stretched = rebalance_outline(outline, 105)
    assert sum(s.target_seconds for s in stretched.scenes) == 105 * 60
    # proportions survive: the longest demo scene is still the longest
    longest = max(outline.scenes, key=lambda s: s.target_seconds).scene_id
    assert max(stretched.scenes, key=lambda s: s.target_seconds).scene_id == longest


def test_validate_outline_accepts_demo(outline):
    assert validate_outline(outline, 3, 0.5) == []


def test_validate_outline_flags_structure(outline):
    scenes = [s.model_copy(update={"set_piece": "none"}) if s.set_piece == "interval_bang" else s
              for s in outline.scenes]
    issues = validate_outline(Outline(scenes=scenes), 3, 0.5)
    assert any("interval_bang" in i for i in issues)
    issues = validate_outline(outline, 10, 1)
    assert any("total runtime" in i for i in issues)


def test_fit_shots_exact_frames_even_past_clamp():
    shots = ShotList.model_validate({"scene_id": "sc004", "shots": SHOTS["sc004"]})   # 10 s + 20 s
    fitted = fit_shots_to_scene(shots, 30, fps=24)
    frames = [round(s.duration_s * 24) for s in fitted.shots]
    assert sum(frames) == 30 * 24
    assert all(abs(s.duration_s * 24 - round(s.duration_s * 24)) < 1e-9 for s in fitted.shots)


def test_fit_shots_scales_down():
    shots = ShotList.model_validate({"scene_id": "sc002", "shots": SHOTS["sc002"]})   # 35 s
    fitted = fit_shots_to_scene(shots, 20, fps=24)
    assert round(sum(s.duration_s for s in fitted.shots) * 24) == 20 * 24


def test_cue_plan_follows_rasa_curve(outline, bible):
    nav = load_navarasa()
    sheet = plan_cues(outline, bible, nav)
    assert [c.rasa for c in sheet.cues] == [s.rasa_primary for s in outline.scenes]
    starts = scene_start_times(outline)
    for cue in sheet.cues:
        assert cue.start_s == starts[cue.scene_ids[0]]
        assert cue.raga in nav["rasas"][cue.rasa]["music"]["ragas"]
        lo, hi = nav["rasas"][cue.rasa]["music"]["tempo_bpm"]
        assert lo <= cue.bpm <= hi
    peaks = [c for c in sheet.cues if c.peak]
    assert {c.cue_type for c in peaks} == {"hero_elevation", "interval_bang", "climax"}
    assert all(c.silence_before_s > 0 for c in peaks)


def test_cue_plan_merges_adjacent_same_rasa(bible):
    base = OUTLINE["scenes"][2]   # shringara, 30 s
    scenes = [{**base, "scene_id": f"sc{i:03d}"} for i in range(1, 4)]
    sheet = plan_cues(Outline.model_validate({"scenes": scenes}), bible, load_navarasa())
    assert len(sheet.cues) == 1
    assert sheet.cues[0].duration_s == 90


def test_revisions_cannot_introduce_unknown_raga(outline, bible):
    nav = load_navarasa()
    sheet = plan_cues(outline, bible, nav)
    revs = CueRevisions.model_validate(CUE_REVISIONS).revisions
    bad = revs[0].model_copy(update={"cue_id": "cue002", "raga": "made_up_raga"})
    revised, notes = apply_revisions(sheet, [*revs, bad], nav)
    by_id = {c.cue_id: c for c in revised.cues}
    assert by_id["cue005"].raga == "subhapantuvarali"
    assert by_id["cue002"].raga == "nattai"
    assert any("made_up_raga" in n for n in notes)
    # timing is never editable
    assert [c.start_s for c in revised.cues] == [c.start_s for c in sheet.cues]
