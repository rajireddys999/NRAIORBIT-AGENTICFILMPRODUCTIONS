"""`produce`: the whole row on the recorded agents and mock providers, gates by policy, a report at the end."""

import json

import pytest
import yaml

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
from nraiorbit.pipeline import Pipeline
from nraiorbit.post import ffmpeg
from nraiorbit.service import produce

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")


def _pipeline(tmp_path, gates="ask"):
    root = tmp_path / "demo"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = deep_merge(DEMO_CONFIG, {"review_gates": {g: {"enabled": True, "auto_approve": False} for g in ("story", "cast")},
                                   "policy": {"gates": gates, "publish": "none"}})
    (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda *_: None)


def test_ask_policy_stops_at_the_first_gate_and_resumes_after_approval(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    p = _pipeline(tmp_path, gates="ask")
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "awaiting:story" and (p.p.reviews / "story.pending").exists()
    produce.approve(p, "story")
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "awaiting:cast"
    produce.approve(p, "cast")
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "done", report
    assert report["steps"]["plan"] == "done" and report["steps"]["render"] == "done"
    assert report["steps"]["faces"] == "not needed"                      # mock video: no first frames to check
    assert report["steps"]["deliver"]["bucket"] is None and len(report["steps"]["deliver"]["files"]) >= 2
    assert report["steps"]["publish"] == "none" and "backup" not in report["steps"]
    saved = json.loads((p.p.reports / "produce.json").read_text(encoding="utf-8"))
    assert saved["state"] == "done" and saved["ledger_usd"] == 0.0
    assert (p.p.master / "deliver" / "delivery.json").exists()


def test_auto_policy_approves_gates_itself(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    p = _pipeline(tmp_path, gates="auto")
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "done"
    assert (p.p.reviews / "story.approved").read_text().startswith("approved by policy")
    assert (p.p.reviews / "cast.approved").exists()


def test_policy_overrides_and_faces_gate_rule(tmp_path):
    cfg = {"policy": {"gates": "auto", "publish": "private", "deliver_days": 3}}
    assert produce.policy_of(cfg)["deliver_days"] == 3
    assert produce.policy_of(cfg, gates="ask", publish=None)["gates"] == "ask"
    assert produce.policy_of({})["publish"] == "none"
    p = _pipeline(tmp_path)
    assert not produce._faces_needed(p)                                  # mock video provider
    p.cfg["providers"]["video"] = "veo"
    p.cfg["google"] = {"veo_mode": "keyframe"}
    assert produce._faces_needed(p)
    p.p.reviews.mkdir(parents=True)
    (p.p.reviews / "faces.approved").write_text("ok")
    assert not produce._faces_needed(p)
