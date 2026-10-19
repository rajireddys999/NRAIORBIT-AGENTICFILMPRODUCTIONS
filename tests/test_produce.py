"""`produce`: the whole row on the recorded agents and mock providers, gates by policy, a report at the end."""

import hashlib
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


def test_a_fresh_container_with_only_the_backed_up_sets_still_cuts_the_trailer(tmp_path, monkeypatch):
    """The incident: a tick stops at the pacing gate and backs the project up; the approval tick starts on an empty
    disk with only the backed-up sets (no 10_edit, no 07_video, no 05_dialogue). The render step re-makes the
    timeline from the caches, assembly masters again because the trailer's source is gone, the cards go on the new
    master once (never a second time over the restored, already carded one), and the trailer is cut."""
    import shutil

    from nraiorbit.ops import backup
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    root = tmp_path / "demo"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    title = {"kind": "title", "at_s": 1.0, "length_s": 1.5, "text_en": "MONKEY SAVES MAN"}
    cfg = deep_merge(DEMO_CONFIG, {"review_gates": {"pacing": {"enabled": True, "auto_approve": False}},
                                   "policy": {"gates": "ask", "publish": "none"}, "cards": [title]})
    (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    p = Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda *_: None)
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "awaiting:pacing" and (p.p.master / "movie.mp4").exists()
    assert (p.p.edit / "film_dialogue.mp4").exists() and not (p.p.master / "trailer.mp4").exists()
    assert (p.p.edit / "movie_nocards.mp4").exists()                     # the master in 11_master carries the card
    restored_master = hashlib.sha256((p.p.master / "movie.mp4").read_bytes()).hexdigest()
    fresh = tmp_path / "fresh" / "demo"                                  # what a new container restores, no more
    for kind, local, _ in backup.pairs(root, "bucket"):
        if not local.exists():
            continue
        target = fresh / local.relative_to(root)
        if kind == "folder":
            shutil.copytree(local, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, target)
    assert not (fresh / "10_edit").exists() and not (fresh / "07_video").exists()
    q = Pipeline(fresh, demo_llm(), cfg=load_pipeline_config(fresh), log=lambda *_: None)
    produce.approve(q, "pacing")
    report = produce.produce(q, log=lambda *_: None)
    assert report["state"] == "done", report
    assert (q.p.master / "trailer.mp4").exists() and (q.p.edit / "film_dialogue.mp4").exists()
    carded = json.loads((q.p.edit / "cards.json").read_text(encoding="utf-8"))
    assert [c["kind"] for c in carded] == ["title"] and carded[0]["text_en"] == "MONKEY SAVES MAN"
    clean = q.p.edit / "movie_nocards.mp4"                               # carded once, from the fresh clean master:
    assert clean.exists() and hashlib.sha256(clean.read_bytes()).hexdigest() != restored_master
    # (a second card pass over the restored master would keep its bytes as the "clean" copy, shutil.move does)
    assert report["ledger_usd"] == 0.0                                   # nothing paid on the way


def test_every_gate_listed_stops_at_each_step_the_cost_and_the_upload(tmp_path, monkeypatch):
    """The control room's template: every step waits for a look, the estimate is approved before anything is paid,
    and the YouTube upload is approved once the film can be watched. Each stop leaves the marker the queue approves."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    every = ["story", "outline", "scenes", "cast", "shots", "render", "pacing", "final_cut", "trailer"]
    root = tmp_path / "demo"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = deep_merge(DEMO_CONFIG, {"review_gates": {g: {"enabled": True, "auto_approve": False} for g in every},
                                   "policy": {"gates": "ask", "publish": "private"}})
    (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    p = Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda *_: None)
    monkeypatch.setattr(produce, "cost_gate", lambda pipeline: (None, {"total_usd": 12.5, "video_usd": 10.0, "tts_usd": 2.5}))
    uploads = []
    monkeypatch.setattr(produce, "publish_youtube", lambda root, privacy: uploads.append(privacy) or {"film": {"id": "vid1"}})
    seen = []
    for _ in range(14):
        report = produce.produce(p, log=lambda *_: None)
        if not report["state"].startswith("awaiting:"):
            break
        gate = report["state"][len("awaiting:"):]
        seen.append(gate)
        assert (p.p.reviews / f"{gate}.pending").exists(), gate
        if gate == "cost":
            assert json.loads((p.p.reviews / "cost.pending").read_text())["total_usd"] == 12.5
            assert report["steps"]["estimate"]["total_usd"] == 12.5 and not uploads
        produce.approve(p, gate)
    assert report["state"] == "done", report
    assert seen[:6] == ["story", "outline", "scenes", "cast", "shots", "cost"]
    assert seen[6] == "render" and seen[7] in ("pacing", "final_cut") and seen[8:] == ["trailer", "publish"]
    assert report["steps"]["cost"] == "approved" and uploads == ["private"]
    assert report["steps"]["publish"] == {"film": {"id": "vid1"}}
