"""One story, start to finish, under the project's policy (`nraiorbit produce <slug>`).

    plan (story bible … shots, cues)  →  faces (first frames, checked before video is paid)  →  render (picture,
    voices, music, lip-sync under the cap)  →  assemble, trailer  →  QA (lip-sync sheets, film check)  →  deliver
    (720p, bucket, links)  →  publish (YouTube, private by default)  →  backup

`policy` in the project's pipeline.yaml says how far it goes on its own:

    policy:
      gates: ask | auto        ask: stop at every review gate (story, cast, faces, pacing, trailer) and report
                               awaiting:<gate>; auto: approve them as they come. The budget cap is never touched.
      publish: none | private | unlisted | public
      deliver_days: 7
      tag: ""                  a word in the delivery file names

The run stops cleanly with a state the owner can act on: awaiting:<gate>, blocked:<reason> (budget, credits, an API
refusal) or done. Every step's outcome goes to reports/produce.json. Nothing is retried blindly: a second call picks
up where the first stopped because every stage, cache and gate is on disk.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace

from ..costing import BudgetExceeded, SpendLedger
from ..pipeline import GatePending, Pipeline, PipelineBlocked

GATE_ORDER = ["story", "cast", "faces", "pacing", "final_cut", "trailer"]


def policy_of(cfg: dict, **overrides) -> dict:
    pol = {"gates": "ask", "publish": "none", "deliver_days": 7, "tag": ""}
    pol.update({k: v for k, v in (cfg.get("policy") or {}).items() if v is not None})
    pol.update({k: v for k, v in overrides.items() if v is not None})
    return pol


def approve(pipeline: Pipeline, gate: str) -> None:
    pipeline.p.reviews.mkdir(parents=True, exist_ok=True)
    (pipeline.p.reviews / f"{gate}.pending").unlink(missing_ok=True)
    (pipeline.p.reviews / f"{gate}.approved").write_text("approved by policy\n", encoding="utf-8")


def _faces_needed(pipeline: Pipeline) -> bool:
    """The faces gate applies when video is paid from first frames and no clips exist yet."""
    cfg = pipeline.cfg
    if cfg.get("providers", {}).get("video") != "veo" or cfg.get("google", {}).get("veo_mode", "keyframe") != "keyframe":
        return False
    if (pipeline.p.reviews / "faces.approved").exists():
        return False
    return not any((pipeline.p.root / "inbox" / "video").glob("*/*.mp4"))


def produce(pipeline: Pipeline, *, bucket: str | None = None, publish: str | None = None, gates: str | None = None,
            keyframes=None, log=print) -> dict:
    """Drive the project as far as its policy allows. `keyframes` is the first-frames step (the CLI's command),
    injected so tests can skip it. Returns and writes the report."""
    root = pipeline.p.root
    pol = policy_of(pipeline.cfg, gates=gates, publish=publish)
    report = {"slug": root.name, "started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "policy": pol, "steps": {}, "state": "running"}

    def finish(state: str) -> dict:
        report["state"] = state
        report["ledger_usd"] = round(SpendLedger(pipeline.p.reports / "cost.jsonl", None).spent(), 2)
        report["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        pipeline.p.reports.mkdir(parents=True, exist_ok=True)
        (pipeline.p.reports / "produce.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        log(f"  produce: {state}")
        return report

    # 1. plan up to the shot lists (Claude only), honouring the story and cast gates
    while True:
        try:
            pipeline.run(until="shots")
            break
        except GatePending as g:
            if pol["gates"] == "auto":
                approve(pipeline, g.gate)
                continue
            report["steps"]["plan"] = f"awaiting:{g.gate}"
            return finish(f"awaiting:{g.gate}")
        except (PipelineBlocked, BudgetExceeded) as b:
            report["steps"]["plan"] = f"blocked: {b}"
            return finish(f"blocked:{str(b)[:120]}")
    report["steps"]["plan"] = "done"

    # 2. faces: first frames for every shot with a lead, then a human look (ask) or straight on (auto)
    if _faces_needed(pipeline) and pol["gates"] == "ask":
        if keyframes is not None:
            try:
                keyframes(SimpleNamespace(slug=root.name, only=None, max_spend=None, yes=True))
            except SystemExit as e:                      # the command reports its own reason
                report["steps"]["faces"] = f"blocked: {e}"
                return finish(f"blocked:{str(e)[:120]}")
        report["steps"]["faces"] = "awaiting:faces"
        return finish("awaiting:faces")
    report["steps"]["faces"] = "approved" if (pipeline.p.reviews / "faces.approved").exists() else "not needed"

    # 3. the paid stages, assembly and trailer under the cap
    while True:
        try:
            pipeline.run()
            break
        except GatePending as g:
            if pol["gates"] == "auto":
                approve(pipeline, g.gate)
                continue
            report["steps"]["render"] = f"awaiting:{g.gate}"
            return finish(f"awaiting:{g.gate}")
        except (PipelineBlocked, BudgetExceeded) as b:
            report["steps"]["render"] = f"blocked: {b}"
            return finish(f"blocked:{str(b)[:120]}")
    report["steps"]["render"] = "done"

    # 4. QA: never stops the run; it writes sheets and a verdict for the owner
    try:
        from ..qa import film
        fc = film.check(root, log=log)
        report["steps"]["qa_film"] = "ok" if fc.get("ok") else "look"
        if pipeline.cfg.get("providers", {}).get("lipsync", "none") != "none":
            from ..qa import lipsync
            rows = lipsync.review(root, log=log)
            report["steps"]["qa_lipsync"] = f"{sum(1 for r in rows if r.get('flags'))} of {len(rows)} shots flagged"
    except Exception as exc:                              # missing mediapipe, no speaking shots: QA is advisory
        report["steps"]["qa"] = f"skipped: {type(exc).__name__}: {str(exc)[:100]}"

    # 5. deliverables and links
    try:
        from ..release import deliver, gcs
        bucket = bucket or (gcs.default_bucket(pipeline.cfg) if os.environ.get("GOOGLE_CLOUD_PROJECT")
                            or (pipeline.cfg.get("lipsync") or {}).get("bucket") else None)
        recs = deliver.deliver(root, bucket, tag=pol.get("tag", ""), days=int(pol.get("deliver_days", 7)),
                               upload=bucket is not None, log=log)
        report["steps"]["deliver"] = {"files": [r["name"] for r in recs], "bucket": bucket,
                                      "links": [r.get("url") for r in recs if r.get("url")]}
    except Exception as exc:
        report["steps"]["deliver"] = f"failed: {type(exc).__name__}: {str(exc)[:160]}"
        return finish("blocked:deliver")

    # 6. YouTube, private unless the policy says otherwise; skipped without credentials
    if pol["publish"] in ("private", "unlisted", "public"):
        try:
            report["steps"]["publish"] = publish_youtube(root, pol["publish"])
        except Exception as exc:
            report["steps"]["publish"] = f"skipped: {type(exc).__name__}: {str(exc)[:160]}"
    else:
        report["steps"]["publish"] = "none"

    # 7. backup
    if bucket:
        try:
            from ..ops import backup
            report["steps"]["backup"] = f"{backup.backup(root, bucket, log=log)} sets"
        except Exception as exc:
            report["steps"]["backup"] = f"failed: {type(exc).__name__}: {str(exc)[:160]}"
    return finish("done")


def publish_youtube(root: Path, privacy: str) -> dict:
    """Upload the film (and the trailer when there is one) with the metadata the outline gives. The OAuth client
    secrets come from YOUTUBE_CLIENT_SECRETS (default client_secret.json next to the project) and the token, made
    once on a laptop, from YOUTUBE_TOKEN (default <project>/.youtube_token.json)."""
    from ..publish import youtube
    from ..schemas import Outline, StoryBible

    secrets = Path(os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secret.json"))
    token = Path(os.environ.get("YOUTUBE_TOKEN", str(root / ".youtube_token.json")))
    if not secrets.exists() and not token.exists():
        raise FileNotFoundError("no YouTube credentials (YOUTUBE_CLIENT_SECRETS / YOUTUBE_TOKEN)")
    bible = StoryBible.model_validate_json((root / "01_story_bible.json").read_text(encoding="utf-8"))
    outline = Outline.model_validate_json((root / "02_outline.json").read_text(encoding="utf-8"))
    creds = youtube.credentials(secrets, token)
    master = root / "11_master"
    out = {}
    meta = youtube.build_metadata(bible, outline, privacy=privacy)
    captions = {lang: master / f"subs_{lang}.srt" for lang in ("te", "en") if (master / f"subs_{lang}.srt").exists()}
    thumb = master / "thumbnail.png"
    out["film"] = youtube.upload(master / "movie.mp4", meta, creds, captions=captions,
                                 thumbnail=thumb if thumb.exists() else None)
    if (master / "trailer.mp4").exists():
        tmeta = youtube.build_metadata(bible, outline, privacy=privacy)
        tmeta["snippet"]["title"] = f"{bible.title_te} ({bible.title_en}) | Official Trailer"[:100]
        out["trailer"] = youtube.upload(master / "trailer.mp4", tmeta, creds)
    return out
