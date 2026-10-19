"""The review gates as files: the step gates fire only when a project lists them, and the runner's own gates
(cost, faces, publish) leave the same pending marker a pipeline gate does, so an approval from the control room
or the queue sheet reaches the next run."""

from types import SimpleNamespace

import pytest

from nraiorbit.pipeline import STAGE_GATES, STEP_GATES, GatePending, Pipeline, ProjectPaths
from nraiorbit.service import produce


def _p(tmp_path, review_gates):
    return SimpleNamespace(cfg={"review_gates": review_gates}, p=ProjectPaths(tmp_path))


def test_step_gates_are_opt_in_and_the_classic_gates_are_not(tmp_path):
    for name in STEP_GATES:
        Pipeline.gate(_p(tmp_path, {}), name)                                  # unlisted: no stop, no marker
    assert not (tmp_path / "reviews").exists()
    with pytest.raises(GatePending):
        Pipeline.gate(_p(tmp_path, {"outline": {"auto_approve": False}}), "outline")
    assert (tmp_path / "reviews" / "outline.pending").exists()
    Pipeline.gate(_p(tmp_path, {"outline": {"auto_approve": True}}), "outline")  # listed but waved through
    with pytest.raises(GatePending):
        Pipeline.gate(_p(tmp_path, {}), "story")                                 # the classic gates stop as before
    (tmp_path / "reviews" / "story.approved").write_text("ok")
    Pipeline.gate(_p(tmp_path, {}), "story")                                     # approved: through
    assert STEP_GATES | {"story", "cast", "pacing", "trailer"} <= set(STAGE_GATES.values())


def test_runner_gates_leave_the_same_marker_and_read_the_approval(tmp_path):
    p = _p(tmp_path, {})
    produce.hold(p, "cost", '{"total_usd": 1.0}\n')
    assert (tmp_path / "reviews" / "cost.pending").read_text().startswith("{") and not produce.approved(p, "cost")
    produce.approve(p, "cost")
    assert produce.approved(p, "cost") and not (tmp_path / "reviews" / "cost.pending").exists()
    assert produce.GATE_ORDER[:6] == ["story", "outline", "scenes", "cast", "shots", "cost"]
    assert produce.GATE_ORDER[-1] == "publish" and set(produce.RUNNER_GATES) == {"cost", "faces", "publish"}


def test_the_queue_approves_the_gate_the_run_stopped_at_never_a_spent_marker(tmp_path):
    from nraiorbit.service import queue
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    for n in ("story.pending", "story.approved", "outline.pending", "outline.approved", "scenes.pending"):
        (reviews / n).write_text("x")
    assert queue.pending_gate(tmp_path) == "scenes"                      # the only marker never approved
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "produce.json").write_text('{"state": "awaiting:outline"}')
    assert queue.pending_gate(tmp_path) == "outline"                     # the run's own state wins (a re-armed gate)
    (tmp_path / "reports" / "produce.json").write_text('{"state": "awaiting:nothing"}')
    assert queue.pending_gate(tmp_path) == "scenes"
    queue.approve_gate(tmp_path, "scenes")
    assert (reviews / "scenes.approved").read_text() == "approved in the queue\nx" and not (reviews / "scenes.pending").exists()


def test_a_bucket_that_keeps_spent_markers_does_not_deadlock_the_queue(tmp_path, monkeypatch):
    """Cloud Run starts every tick on an empty disk and restores the project from the bucket, and a mirror can bring
    back a marker the runner already consumed. The row is claimed before the restore, the approval goes to the gate
    the run stopped at and never to a spent marker, and a film that stops at every step still reaches done."""
    import json
    import shutil

    from nraiorbit.ops import backup
    from nraiorbit.service import queue
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "rain", "story_uri": str(tmp_path / "rain.md"), "budget_usd": "40", "gates": "ask",
                          "publish": "none", "state": "new", "note": "", "updated": ""}], queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    projects, bucket = tmp_path / "projects", tmp_path / "bucket"
    template = projects / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    monkeypatch.setattr(backup, "restore", lambda root, b, log=None: (bucket / root.name).exists()
                        and shutil.copytree(bucket / root.name, root, dirs_exist_ok=True))
    monkeypatch.setattr(backup, "backup",
                        lambda root, b, log=None: shutil.copytree(root, bucket / root.name, dirs_exist_ok=True))
    monkeypatch.setattr(backup, "sync_library", lambda *a, **k: None)
    order = ["story", "outline", "scenes", "cast"]

    def produce_fn(root, b):                     # stops at each gate in turn, as produce does, and writes its report
        assert queue.load(str(st), queue.STORY_COLUMNS)[0]["state"] == "producing"   # claimed before any work
        reviews = root / "reviews"
        reviews.mkdir(exist_ok=True)
        state = "done"
        for g in order:
            if not (reviews / f"{g}.approved").exists():
                (reviews / f"{g}.pending").write_text(f"look at {g}\n")
                state = f"awaiting:{g}"
                break
        (root / "reports").mkdir(exist_ok=True)
        (root / "reports" / "produce.json").write_text(json.dumps({"state": state, "steps": {}}))
        return {"state": state, "steps": {}}

    seen = []
    for _ in range(12):
        shutil.rmtree(projects / "rain", ignore_errors=True)              # a fresh container's empty disk
        queue.tick(str(st), str(rf), projects, bucket="b", produce_fn=produce_fn, learn_fn=None, template=template,
                   log=lambda *_: None)
        rows = queue.load(str(st), queue.STORY_COLUMNS)
        seen.append(rows[0]["state"])
        if rows[0]["state"] == "done":
            break
        rows[0]["state"] = "approved"
        queue.save(str(st), rows, queue.STORY_COLUMNS)
    assert seen == ["awaiting:story", "awaiting:outline", "awaiting:scenes", "awaiting:cast", "done"]
    marks = {p.name for p in (bucket / "rain" / "reviews").iterdir()}
    assert "story.pending" in marks and "cast.approved" in marks         # the stale marker stayed and did no harm
    assert (bucket / "rain" / "reviews" / "cast.approved").read_text().endswith("look at cast\n")


def test_a_producing_row_is_another_ticks_while_its_claim_is_fresh(monkeypatch):
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    fresh = {"slug": "a", "state": "producing", "updated": queue.now()}
    stale = {"slug": "b", "state": "producing", "updated": "2026-01-01T00:00:00+00:00"}
    new = {"slug": "c", "state": "new", "updated": ""}
    assert queue.ready_story([fresh]) is None and queue.ready_story([stale]) is stale
    assert queue.ready_story([fresh, new]) is new
    monkeypatch.setenv("NRAIORBIT_CLAIM_HOURS", "0")
    assert queue.ready_story([fresh]) is fresh


def test_invalidating_a_stage_clears_the_runner_gates_it_makes_stale(tmp_path):
    p = _p(tmp_path, {})
    p.p.reviews.mkdir()
    for n in ("cost.approved", "faces.approved", "publish.approved", "story.approved"):
        (p.p.reviews / n).write_text("x")
    Pipeline.invalidate(p, "trailer")
    assert not (p.p.reviews / "publish.approved").exists() and (p.p.reviews / "cost.approved").exists()
    Pipeline.invalidate(p, "render")
    assert not (p.p.reviews / "cost.approved").exists() and not (p.p.reviews / "faces.approved").exists()
    assert (p.p.reviews / "story.approved").exists()


def test_the_cost_approval_remembers_the_figure(tmp_path):
    p = _p(tmp_path, {})
    produce.hold(p, "cost", '{"total_usd": 12.5}\n')
    produce.approve(p, "cost", "approved in the queue\n" + (tmp_path / "reviews" / "cost.pending").read_text())
    assert produce.approved_figure(p, "cost") == 12.5
    produce.approve(p, "cost")
    assert produce.approved_figure(p, "cost") is None                     # an old approval carries no figure


def test_the_backup_mirrors_the_review_markers_with_deletes(tmp_path, monkeypatch):
    from pathlib import Path

    from nraiorbit.ops import backup
    from nraiorbit.release import gcs
    calls = []
    monkeypatch.setattr(gcs, "rsync", lambda src, dst, delete=False: calls.append((Path(src).name, delete)))
    monkeypatch.setattr(gcs, "copy", lambda src, dst: None)
    (tmp_path / "reviews").mkdir()
    (tmp_path / "reports").mkdir()
    backup.backup(tmp_path, "bkt", log=lambda *_: None)
    assert ("reviews", True) in calls and ("reports", False) in calls
