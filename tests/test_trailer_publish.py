from nraiorbit.demo import OUTLINE, SHOTS, STORY_BIBLE
from nraiorbit.planning.runtime import fit_shots_to_scene, scene_start_times
from nraiorbit.post.subtitles import write_srt
from nraiorbit.post.trailer import CLIMAX_REGION, SPOILER_CUTOFF, plan_trailer
from nraiorbit.publish.youtube import build_metadata, chapters
from nraiorbit.schemas import Outline, ShotList, StoryBible


def _film_shots():
    outline = Outline.model_validate(OUTLINE)
    starts = scene_start_times(outline)
    out = []
    for sc in outline.scenes:
        t = starts[sc.scene_id]
        fitted = fit_shots_to_scene(ShotList.model_validate({"scene_id": sc.scene_id, "shots": SHOTS[sc.scene_id]}),
                                    sc.target_seconds)
        for shot in fitted.shots:
            out.append((shot, t))
            t += shot.duration_s
    return out, sum(s.target_seconds for s in outline.scenes)


def test_trailer_respects_budget_and_spoilers():
    shots, total = _film_shots()
    starts = {s.shot_id: t for s, t in shots}
    plan = plan_trailer(shots, StoryBible.model_validate(STORY_BIBLE), total, 30)
    assert sum(c.duration_s for c in plan) <= 30 + 0.5
    assert sum(c.section == "title" for c in plan) == 1
    ids = [c.shot_id for c in plan if c.shot_id]
    assert len(ids) == len(set(ids))
    for clip in plan:
        if clip.shot_id is None:
            continue
        pos = starts[clip.shot_id] / total
        assert pos <= SPOILER_CUTOFF
        if pos > CLIMAX_REGION:
            assert clip.section == "escalation" and clip.duration_s <= 0.8
    assert any(c.section == "punch" for c in plan)


def test_youtube_metadata_discloses_synthetic_media():
    meta = build_metadata(StoryBible.model_validate(STORY_BIBLE), Outline.model_validate(OUTLINE))
    assert meta["status"]["containsSyntheticMedia"] is True
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["snippet"]["defaultAudioLanguage"] == "te"
    assert len(meta["snippet"]["title"]) <= 100


def test_chapters_start_at_zero():
    marks = chapters(Outline.model_validate(OUTLINE))
    assert marks[0] == (0.0, "Opening")
    assert len(marks) >= 3
    assert all(b[0] - a[0] >= 10 for a, b in zip(marks, marks[1:]))


def test_srt_format(tmp_path):
    out = write_srt([(65.5, 70.25, "రెండు"), (1.0, 2.5, "ఒకటి")], tmp_path / "s.srt")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("1\n00:00:01,000 --> 00:00:02,500\nఒకటి\n")
    assert "2\n00:01:05,500 --> 00:01:10,250\nరెండు" in text
