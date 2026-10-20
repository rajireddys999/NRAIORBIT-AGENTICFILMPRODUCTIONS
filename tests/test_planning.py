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


# ------------------------------------------------------------------------------------- the billable grid

def test_grid_fit_lands_every_shot_on_a_billed_length_and_hits_the_target():
    from nraiorbit.planning.runtime import fit_shots_to_grid
    shots = ShotList.model_validate({"scene_id": "sc002", "shots": SHOTS["sc002"]})     # 35 s as planned
    n = len(shots.shots)
    for target in range(4 * n, 8 * n + 1, 2):
        fitted = fit_shots_to_grid(shots, target, [4, 6, 8])
        assert fitted is not None, target
        assert all(s.duration_s in (4.0, 6.0, 8.0) for s in fitted.shots)
        assert sum(s.duration_s for s in fitted.shots) == target
        assert [s.shot_id for s in fitted.shots] == [s.shot_id for s in shots.shots]
    # proportions survive: the longest planned shot is never shorter than the shortest planned one
    fitted = fit_shots_to_grid(shots, 6 * n, [4, 6, 8])
    longest = max(shots.shots, key=lambda s: s.duration_s).shot_id
    shortest = min(shots.shots, key=lambda s: s.duration_s).shot_id
    by_id = {s.shot_id: s.duration_s for s in fitted.shots}
    assert by_id[longest] >= by_id[shortest]
    # out of reach: an odd target, too few or too many shots, a 1080p-only grid that does not divide; never a loop
    assert fit_shots_to_grid(shots, 6 * n + 1, [4, 6, 8]) is None
    assert fit_shots_to_grid(shots, 4 * n - 2, [4, 6, 8]) is None
    assert fit_shots_to_grid(shots, 8 * n + 2, [4, 6, 8]) is None
    assert fit_shots_to_grid(shots, 8 * n - 2, [8]) is None and fit_shots_to_grid(shots, 8 * n, [8]) is not None
    assert fit_shots_to_grid(shots, 4 * n + 2, [4, 8]) is None          # 4s and 8s cannot make 4n+2
    # a per-shot grid: one shot is 1080p (8 s only)
    first = shots.shots[0].shot_id
    fitted = fit_shots_to_grid(shots, 6 * n, lambda s: [8] if s.shot_id == first else [4, 6, 8])
    assert fitted.shots[0].duration_s == 8.0 and sum(s.duration_s for s in fitted.shots) == 6 * n


def test_rebalance_on_an_even_step_keeps_the_runtime_and_even_scenes(outline):
    even = rebalance_outline(outline, 10, step=2)
    assert sum(s.target_seconds for s in even.scenes) == 600
    assert all(s.target_seconds % 2 == 0 for s in even.scenes)
    plain = rebalance_outline(outline, 10)
    assert sum(s.target_seconds for s in plain.scenes) == 600
    assert all(abs(a.target_seconds - b.target_seconds) <= 2 for a, b in zip(even.scenes, plain.scenes))


def thirteen_scenes(outline, lengths):
    first = outline.scenes[0]
    return Outline(scenes=[first.model_copy(update={"scene_id": f"sc{i + 1:03d}", "target_seconds": t})
                           for i, t in enumerate(lengths)])


def test_many_scenes_in_a_short_film_get_a_floor_that_fits_never_a_negative_scene(outline):
    """13 scenes in a 3-minute film: 13 x 20 s would not fit, so the 20 s floor gave way by pushing one scene to -60 s
    (its shots one frame each). The floor now fits the runtime and the drift is spread, never below it."""
    live = thirteen_scenes(outline, [-60] + [20] * 12)                   # the live film's outline, as rebalanced before
    fixed = rebalance_outline(live, 3, step=2)
    lengths = [s.target_seconds for s in fixed.scenes]
    assert sum(lengths) == 180 and min(lengths) >= 12 and all(t % 2 == 0 for t in lengths)
    planned = rebalance_outline(thirteen_scenes(outline, [10, 25, 15, 30, 12, 18, 20, 22, 16, 14, 26, 40, 20]), 3, step=2)
    lengths = [s.target_seconds for s in planned.scenes]
    assert sum(lengths) == 180 and min(lengths) >= 12 and all(t % 2 == 0 for t in lengths)
    roomy = rebalance_outline(thirteen_scenes(outline, [30] * 13), 10, step=2)   # room for 20 s each: the floor holds
    assert min(s.target_seconds for s in roomy.scenes) >= 20 and sum(s.target_seconds for s in roomy.scenes) == 600


def test_the_director_hears_the_clip_lengths_and_the_shot_budget(outline, bible):
    from nraiorbit.agents.production import Director, most_shots
    from nraiorbit.schemas import CastLock, SceneScript
    seen = []

    class Recorder:
        def generate(self, *, schema, system, context, task, effort, agent):
            seen.append(task)
            return ShotList(scene_id="sc001", shots=[])
    director = Director(Recorder(), {})
    sc = outline.scenes[0].model_copy(update={"target_seconds": 20})
    script = SceneScript(scene_id="sc001", heading="h", synopsis="s", rasa_primary=sc.rasa_primary, intensity=5,
                         blocks=[])
    director.shots(bible, CastLock(characters={}), sc, script, [], {})
    director.shots(bible, CastLock(characters={}), sc, script, [], {}, clip_lengths=[4, 6, 8], issue="too many")
    assert "at most" not in seen[0]                                       # no grid: the plan as before
    assert "between 3 and 5 shots, each duration_s one of 4, 6, 8, adding up to 20 s" in seen[1]
    assert "could not be used: too many" in seen[1]
    assert most_shots(20, [4, 6, 8]) == 5 and most_shots(-60, [4, 6, 8]) == 1 and most_shots(14, [8]) == 1
    from nraiorbit.agents.production import least_shots, shots_fit
    assert least_shots(36, [4, 6, 8]) == 5 and not shots_fit(3, 36, [4, 6, 8]) and shots_fit(5, 36, [4, 6, 8])
    assert least_shots(14, [8]) == 1                                      # never more than the most


def test_a_shot_list_too_long_for_the_clip_grid_is_asked_for_once_more(tmp_path, outline, bible):
    from types import SimpleNamespace

    from nraiorbit.pipeline import Pipeline, ProjectPaths
    from nraiorbit.schemas import CastLock, SceneScript
    paths = ProjectPaths(tmp_path)
    sc = outline.scenes[0].model_copy(update={"target_seconds": 20})
    paths.outline.parent.mkdir(parents=True, exist_ok=True)
    paths.outline.write_text(Outline(scenes=[sc]).model_dump_json(), encoding="utf-8")
    paths.bible.write_text(bible.model_dump_json(), encoding="utf-8")
    paths.scenes.mkdir(parents=True)
    script = SceneScript(scene_id=sc.scene_id, heading="h", synopsis="s", rasa_primary=sc.rasa_primary, intensity=5,
                         blocks=[])
    (paths.scenes / f"{sc.scene_id}.json").write_text(script.model_dump_json(), encoding="utf-8")
    one = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"][:1]}).shots[0]
    calls = []

    def plan(n):
        return ShotList(scene_id=sc.scene_id, shots=[one.model_copy(update={"shot_id": f"{sc.scene_id}_s{i:03d}"})
                                                     for i in range(1, n + 1)])

    def shots(bible, lock, scene, script, actions, camera, clip_lengths=None, issue=""):
        calls.append((clip_lengths, issue))
        return plan(9 if not issue else 3)
    saved = {}
    p = SimpleNamespace(p=paths, cfg={}, log=lambda *_: None, director=SimpleNamespace(shots=shots),
                        _load=lambda path, model: model.model_validate_json(path.read_text()),
                        _lock=lambda: CastLock(characters={}), library=SimpleNamespace(search=lambda *a, **k: []),
                        navarasa={"rasas": {sc.rasa_primary: {"camera": {}}}}, _grid_lengths=lambda: [4, 6, 8],
                        _clean_shots=lambda shots, script, lock: shots, _fit_shots=lambda shots, sc: shots,
                        _save=lambda path, model: saved.update({path.name: model}))
    Pipeline._shots(p)
    assert [c[0] for c in calls] == [[4, 6, 8], [4, 6, 8]] and "9 shots" in calls[1][1] and "between 3 and 5" in calls[1][1]
    assert len(saved[f"{sc.scene_id}.json"].shots) == 3                   # the second plan, which fits the grid


def test_a_failed_second_ask_keeps_the_first_plan_and_every_other_scene(tmp_path, outline, bible):
    """A refusal or an API error on the second ask must not lose the scene, nor the scenes planned beside it: the
    first plan is fitted as before and the failure is reported."""
    from types import SimpleNamespace

    from nraiorbit.pipeline import Pipeline, ProjectPaths
    from nraiorbit.schemas import CastLock, SceneScript
    paths = ProjectPaths(tmp_path)
    scenes = [outline.scenes[0].model_copy(update={"scene_id": sid, "target_seconds": 20}) for sid in ("sc001", "sc002")]
    paths.outline.parent.mkdir(parents=True, exist_ok=True)
    paths.outline.write_text(Outline(scenes=scenes).model_dump_json(), encoding="utf-8")
    paths.bible.write_text(bible.model_dump_json(), encoding="utf-8")
    paths.scenes.mkdir(parents=True)
    for sc in scenes:
        script = SceneScript(scene_id=sc.scene_id, heading="h", synopsis="s", rasa_primary=sc.rasa_primary,
                             intensity=5, blocks=[])
        (paths.scenes / f"{sc.scene_id}.json").write_text(script.model_dump_json(), encoding="utf-8")
    one = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"][:1]}).shots[0]

    def shots(bible, lock, scene, script, actions, camera, clip_lengths=None, issue=""):
        if issue:
            raise RuntimeError("refused")
        n = 9 if scene.scene_id == "sc001" else 4                         # sc001 off the grid, sc002 on it
        return ShotList(scene_id=scene.scene_id, shots=[one.model_copy(update={"shot_id": f"{scene.scene_id}_s{i}"})
                                                        for i in range(n)])
    saved, reported = {}, []
    p = SimpleNamespace(p=paths, cfg={}, log=lambda *_: None, director=SimpleNamespace(shots=shots),
                        _load=lambda path, model: model.model_validate_json(path.read_text()),
                        _lock=lambda: CastLock(characters={}), library=SimpleNamespace(search=lambda *a, **k: []),
                        navarasa={"rasas": {scenes[0].rasa_primary: {"camera": {}}}}, _grid_lengths=lambda: [4, 6, 8],
                        _clean_shots=lambda shots, script, lock: shots, _fit_shots=lambda shots, sc: shots,
                        _save=lambda path, model: saved.update({path.name: model}),
                        _report=lambda name, record: reported.append((name, record)))
    Pipeline._shots(p)
    assert len(saved["sc001.json"].shots) == 9 and len(saved["sc002.json"].shots) == 4
    assert reported and reported[0][1]["scene_id"] == "sc001" and "failed (RuntimeError)" in reported[0][1]["billable_grid"]


def test_the_balancer_reaches_an_odd_minimum_and_places_an_odd_second(outline):
    four = Outline(scenes=[outline.scenes[0].model_copy(update={"scene_id": f"sc{i}", "target_seconds": 15})
                           for i in range(4)])
    assert sum(s.target_seconds for s in rebalance_outline(four, 1, min_scene_s=15, step=2).scenes) == 60
    odd = rebalance_outline(thirteen_scenes(outline, [12] * 13), 181 / 60, step=2)        # 181 s: one odd second
    assert sum(s.target_seconds for s in odd.scenes) == 181 and min(s.target_seconds for s in odd.scenes) >= 12
    eights = rebalance_outline(thirteen_scenes(outline, [20] * 13), 4, step=8)            # 1080p: 8 s clips only
    assert all(s.target_seconds % 8 == 0 for s in eights.scenes) and min(s.target_seconds for s in eights.scenes) >= 8


def test_the_outline_steps_by_what_the_clips_can_add_up_to(tmp_path, outline, bible):
    """A project whose default Veo resolution is 1080p bills 8 s clips only: scene targets come in steps of 8 s
    (4, 6 and 8 s clips: steps of 2), so every scene can be filled with whole clips."""
    from types import SimpleNamespace

    from nraiorbit.pipeline import Pipeline, ProjectPaths
    paths = ProjectPaths(tmp_path)
    paths.bible.parent.mkdir(parents=True, exist_ok=True)
    paths.bible.write_text(bible.model_dump_json(), encoding="utf-8")
    for grid, runtime, step in (([8], 4, 8), ([4, 6, 8], 3, 2)):
        paths.outline.unlink(missing_ok=True)
        saved = {}
        p = SimpleNamespace(p=paths, log=lambda *_: None, _report=lambda *a: None, _grid_lengths=lambda g=grid: g,
                            _film={"runtime_minutes": runtime, "runtime_tolerance_minutes": 0.5},
                            _load=lambda path, model: model.model_validate_json(path.read_text()),
                            screenwriter=SimpleNamespace(outline=lambda *a, **k: outline),
                            _save=lambda path, model: saved.update(outline=model))
        Pipeline._outline(p)
        lengths = [s.target_seconds for s in saved["outline"].scenes]
        assert sum(lengths) == runtime * 60 and all(t % step == 0 for t in lengths), (grid, lengths)
