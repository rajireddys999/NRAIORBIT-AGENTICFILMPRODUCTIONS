import re
from dataclasses import replace

import pytest

from nraiorbit.demo import OUTLINE, SCENES, SHOTS, STORY_BIBLE
from nraiorbit.planning.runtime import fit_shots_to_scene, scene_start_times
from nraiorbit.post.subtitles import write_srt
from nraiorbit.post.trailer import (
    CLIMAX_REGION,
    LINE_ORDER,
    LINE_PAD_S,
    PUNCH_DROP_S,
    SPOILER_CUTOFF,
    TrailerLine,
    elevation_arc,
    plan_trailer,
)
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


def _film_lines(shots):
    """The demo's dialogue placed as the pipeline does it (shot start + lead-in, mock TTS length)."""
    blocks = {b["block_id"]: b for scene in SCENES.values() for b in scene if b["kind"] == "dialogue"}
    return [TrailerLine(bid, round(t + 0.2, 3), min(8.0, max(0.8, len(b["text_te"]) / 11)), b["character_id"],
                        b["delivery"]["rasa"], b["delivery"]["intensity"])
            for shot, t in shots for bid in shot.block_ids if (b := blocks.get(bid))]


def test_trailer_carries_whole_lines_never_chopped():
    shots, total = _film_shots()
    lines = {ln.line_id: ln for ln in _film_lines(shots)}
    roles = {c["id"]: c["role"] for c in STORY_BIBLE["characters"]}
    plan = plan_trailer(shots, StoryBible.model_validate(STORY_BIBLE), total, 45, list(lines.values()))
    assert sum(c.duration_s for c in plan) <= 45 + 0.5
    carried = [c for c in plan if c.line_id]
    assert {c.section for c in carried} == {"hero", "conflict", "emotion", "punch"} <= set(LINE_ORDER)
    for c in carried:
        line = lines[c.line_id]
        assert line.duration_s <= 7.5 and c.line_duration_s == pytest.approx(line.duration_s, abs=1e-3)
        assert c.film_start_s == pytest.approx(line.film_start_s - LINE_PAD_S, abs=1e-3)   # the picture under the line
        assert c.line_at_s == pytest.approx(c.trailer_start_s + LINE_PAD_S, abs=1e-3)
        assert c.line_at_s + c.line_duration_s <= c.trailer_start_s + c.duration_s + 1e-6   # whole, inside its clip
        assert c.film_start_s + c.duration_s <= SPOILER_CUTOFF * total
    said = sorted((c.line_at_s, c.line_at_s + c.line_duration_s) for c in carried)
    assert all(a[1] <= b[0] for a, b in zip(said, said[1:]))                                # never two at once
    title = next(i for i, c in enumerate(plan) if c.section == "title")
    assert plan[title - 1].section == "punch" and plan[title - 1].line_id                  # punch line, then title
    assert roles[lines[plan[title - 1].line_id].speaker] == "hero"
    assert any(c.section == "conflict" and roles[lines[c.line_id].speaker] == "villain" for c in carried)
    t = 0.0
    for c in plan:                                         # back to back, whole frames: trailer times match the cut
        assert c.trailer_start_s == pytest.approx(t, abs=2e-3)
        assert c.duration_s * 24 == pytest.approx(round(c.duration_s * 24), abs=0.01)
        t += c.duration_s


def test_trailer_line_limits():
    shots, total = _film_shots()
    bible = StoryBible.model_validate(STORY_BIBLE)
    lines = _film_lines(shots)
    too_long = [replace(ln, duration_s=9.0) if ln.line_id == "sc002_b002" else ln for ln in lines]
    assert "sc002_b002" not in {c.line_id for c in plan_trailer(shots, bible, total, 45, too_long)}
    one = plan_trailer(shots, bible, total, 45, lines, max_lines=1)
    assert [c.section for c in one if c.line_id] == ["punch"]
    assert not any(c.line_id for c in plan_trailer(shots, bible, total, 45, lines, max_line_s=0.5))


def test_trailer_line_stays_on_generated_picture():
    """The shot after the punch line is a placeholder: the line's picture slides back onto generated frames."""
    shots, total = _film_shots()
    bible = StoryBible.model_validate(STORY_BIBLE)
    real = [(s, t) for s, t in shots if s.shot_id != "sc002_s003"]          # sc002_s002 ends at 48.0 s

    def moved(start):
        return [replace(ln, film_start_s=start, duration_s=5.0) if ln.line_id == "sc002_b002" else ln
                for ln in _film_lines(shots)]

    punch = next(c for c in plan_trailer(real, bible, total, 45, moved(44.0)) if c.line_id == "sc002_b002")
    assert punch.shot_id == "sc002_s002" and punch.film_start_s + punch.duration_s <= 48.0 + 1e-6
    assert punch.line_at_s + punch.line_duration_s <= punch.trailer_start_s + punch.duration_s + 1e-6
    assert "sc002_b002" not in {c.line_id for c in plan_trailer(real, bible, total, 45, moved(45.5))}  # > 2 s slide


def test_trailer_elevation_arc_has_second_marks_and_the_drop():
    shots, total = _film_shots()
    plan = plan_trailer(shots, StoryBible.model_validate(STORY_BIBLE), total, 45, _film_lines(shots))
    notes, drop = elevation_arc(plan)
    punch = next(c for c in plan if c.section == "punch" and c.line_id)
    title = next(c for c in plan if c.section == "title")
    assert drop == pytest.approx(punch.line_at_s - PUNCH_DROP_S, abs=0.01)
    assert f"{drop:.1f}s: {PUNCH_DROP_S:.1f} s near-silence drop before the punch line" in notes
    assert f"{title.trailer_start_s:.1f}s: huge hit on the title card" in notes
    marks = re.findall(r"(\d+\.\d)-(\d+\.\d)s (\w+)", notes)
    assert marks[0][:1] == ("0.0",) and len(marks) >= 6
    assert [float(a) for a, _, _ in marks] == sorted(float(a) for a, _, _ in marks)
    assert {"world", "hero", "conflict", "emotion", "escalation", "punch"} <= {m[2] for m in marks}


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


def test_trailer_never_shows_a_talking_face_without_its_line():
    """The video model animates the speaker's mouth in a dialogue shot: such a shot is only used with that line."""
    shots, total = _film_shots()
    lines = _film_lines(shots)
    by_id = {s.shot_id: s for s, _ in shots}
    plan = plan_trailer(shots, StoryBible.model_validate(STORY_BIBLE), total, 45, lines)
    speaker = {ln.line_id: ln.speaker for ln in lines}
    starts = {s.shot_id: t for s, t in shots}
    for c in plan:
        if not c.shot_id:
            continue
        for sid, t in starts.items():                     # every shot whose picture this clip shows
            if t < c.film_start_s + c.duration_s - 1e-3 and c.film_start_s < t + by_id[sid].duration_s - 1e-3:
                talking = {b for b in by_id[sid].block_ids if speaker.get(b) in by_id[sid].characters_in_frame}
                assert talking <= ({c.line_id} if c.line_id else set()), (c.shot_id, sid, talking)
    assert any(c.line_id for c in plan)
