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
