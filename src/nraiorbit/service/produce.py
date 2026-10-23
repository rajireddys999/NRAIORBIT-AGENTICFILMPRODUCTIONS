"""One story, start to finish, under the project's policy (`nraiorbit produce <slug>`).

    plan (story bible … shots, cues; a real film's cast reference pictures, a few cents each, under the cap)
    →  cost (the paid stages priced against the cap, approved before any of them is paid)  →  faces (first
    frames, checked before video is paid)  →  render (picture, voices, music, lip-sync under the cap)  →
    assemble, trailer  →  QA (lip-sync sheets, film check)  →  deliver (720p, bucket, links)  →
    publish (YouTube, approved first; private by default)  →  backup

`policy` in the project's pipeline.yaml says how far it goes on its own:

    policy:
      gates: ask | auto        ask: stop at every review gate the project lists (story, outline, scenes, cast, shots,
                               cost, faces, render, pacing, trailer, publish) and report awaiting:<gate>; auto:
                               approve them as they come, except the cost and the YouTube upload, which always wait
                               for the owner (OWNER_GATES). The budget cap is never touched.
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
import re
from pathlib import Path
from types import SimpleNamespace

from ..costing import BudgetExceeded, SpendLedger
from ..pipeline import GatePending, Pipeline, PipelineBlocked
from .queue import GATES as GATE_ORDER

RUNNER_GATES = ("cost", "faces", "publish")          # raised here, between pipeline runs, not inside a stage
OWNER_GATES = ("cost", "publish")                    # money and a release: the owner's under every policy
COST_GROWTH = 0.10                                   # an approved estimate that grows by more than this is asked again


def policy_of(cfg: dict, **overrides) -> dict:
    pol = {"gates": "ask", "publish": "none", "deliver_days": 7, "tag": ""}
    pol.update({k: v for k, v in (cfg.get("policy") or {}).items() if v is not None})
    pol.update({k: v for k, v in overrides.items() if v is not None})
    return pol


# the steps the control room's Send back can name, and the pipeline stage each redoes from
REWORK_STAGES = {"story": "story_bible", "outline": "outline", "scenes": "scenes", "cast": "cast", "shots": "shots",
                 "render": "render", "assemble": "assemble", "trailer": "trailer"}
REWORK_FILE = "rework.json"                           # the tick puts the control room's request here, in the project
SHOT_ID = re.compile(r"^sc\d{3}_s\d{3}$")


def apply_rework(pipeline: Pipeline, log=print) -> dict | None:
    """The owner's Send back, before anything runs, once (its id goes into reports/owner_notes.jsonl): the shots it
    names are planned again from the notes and remade (the video step), or every step from the one it names is
    redone; the notes go to the agents of those steps (a trailer's to the editor, as a trailer direction). The gates
    ask again as each step finishes.

    A Reject (mode "triage", from the film-cut or trailer review) carries only the owner's notes: the editor decides
    what to redo (service/triage). When the editor cannot be reached this raises and the request stays for the next
    run; nothing was undone."""
    from ..pipeline import OWNER_NOTES
    path = pipeline.p.root / REWORK_FILE
    if not path.exists():
        return None
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        req = None
    done = pipeline.p.reports / OWNER_NOTES
    seen = set()
    for line in done.read_text(encoding="utf-8").splitlines() if done.exists() else []:
        try:
            seen.add(json.loads(line).get("id"))
        except ValueError:
            continue
    triage = isinstance(req, dict) and req.get("mode") == "triage"
    stage = REWORK_STAGES.get(str((req or {}).get("stage"))) if isinstance(req, dict) and not triage else None
    notes = str((req or {}).get("notes") or "").strip()[:2000] if isinstance(req, dict) else ""
    if (stage is None and not (triage and notes)) or not req.get("id") or req["id"] in seen:
        path.unlink()
        return None
    shots = [s for s in req.get("shots") or [] if isinstance(s, str) and SHOT_ID.fullmatch(s)]
    decided = {}
    if triage:
        from . import triage as _triage
        decided = _triage.apply(pipeline, req, log)
        stage, shots, notes, what = decided.pop("stage"), decided.pop("shots"), decided.pop("notes"), decided.pop("what")
    elif shots and stage == "render":
        found = pipeline.rework_shots(shots, notes)
        what = f"shots {', '.join(found)} planned again and made anew" if found else "none of those shots exists"
    else:
        from ..pipeline import STAGES
        if stage == "trailer" and notes:                     # the editor turns the notes into a trailer direction
            from .triage import write_direction
            write_direction(pipeline, notes, req["id"])
        if STAGES.index(stage) <= STAGES.index("shots"):     # new plans: no old clip or first frame lands on a shot
            pipeline.fresh_takes(pipeline.made_shots())     # it was not made for (a music redo keeps every clip)
        pipeline.invalidate(stage)
        what = f"every step from {stage} on is redone"
    record = {"id": req["id"], "stage": stage, "shots": shots, "notes": notes, "by": req.get("by"),
              "at": req.get("at"), "applied": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "what": what, **decided}
    pipeline.p.reports.mkdir(parents=True, exist_ok=True)
    cut = done.exists() and not done.read_text(encoding="utf-8").endswith("\n")
    with done.open("a", encoding="utf-8") as f:
        f.write(("\n" if cut else "") + json.dumps(record, ensure_ascii=False) + "\n")
    pipeline._owner_notes()                            # the agents of the steps redone follow the notes
    path.unlink()
    log(f"  {'rejected' if triage else 'sent back'} by {req.get('by') or 'the owner'}: {what}")
    return record


CHILD_NOTES = {
    "shows": ("Google's video model will not show anyone under 18 for this project, so {who} must not be in this shot "
              "at all: not in characters_in_frame, and never named or described in image_prompt or motion_prompt "
              "(write \"a voice from inside\", never their id or name). Keep the story moment: show it through the "
              "other characters' reactions, or the child's voice heard off screen."),
    "names": ("{who} must not appear in this shot (Google's video model will not show anyone under 18 for this "
              "project): write image_prompt and motion_prompt without their id or name (\"a voice from inside\", "
              "\"a sound off screen\"), and do not describe them."),
    "words": ("Google's video model will not show anyone under 18 for this project: no child, kid or schoolchild in "
              "this shot. Keep the story moment with the adults, animals or the place."),
    "animal": ("Google's filter reads a baby held like a human infant as a child and refuses the shot. Draw {who} as "
               "the young animal they are (fur, tail, an animal's body), never called a baby, infant or newborn, and "
               "not cradled, rocked, swaddled or fed like a human infant. Keep the story moment and its feeling."),
    "infant": ("Google's filter reads a baby held like a human infant as a child and refuses the shot: show no baby or "
               "infant held, cradled, rocked or fed. Keep the story moment and its feeling."),
}
CHILD_STOP_KINDS = ("shows", "infant")      # what Google refuses outright: still there after two asks, the run stops
CHILD_ASK_ONCE = ("words",)                 # a child word may be a goat's kid or a banner: asked about once


def _child_note(kind: str, who: str, again: str = "") -> str:
    """What the director is told about a planned shot that shows a child (Pipeline.shots_showing_children): `who` is
    the child (name, age and id) or, for a baby held like an infant, the young animal it is."""
    if kind == "infant" and who:
        kind = "animal"
    retry = f"Your last plan for this shot still {again[len('it '):]}. " if again else ""
    return retry + CHILD_NOTES[kind].format(who=who)


def replan_child_shots(pipeline: Pipeline, log=print) -> tuple[list[str], str]:
    """Shots the video step would still send to Google that show a child (planned before the keep-children-out rule,
    or a slip of the director) are planned again by the director under that rule before anything is paid or sent:
    each moves to a fresh take, so the video step draws a new first frame and, under "ask", the faces check asks
    again. Asked twice, a shot still showing what Google refuses outright (CHILD_STOP_KINDS) stops the run with the
    reason. One that only names a child or uses a child word goes on as planned after two asks. Either way
    reports/child_stops.json keeps the plan it was left with, so later runs neither ask about it again nor (for one
    that goes on) re-plan it, until the plan changes. Returns (the shots planned again, why the run stops or "")."""
    import hashlib
    from ..pipeline import CHILD_STOPS
    from ..schemas import ShotList
    found = pipeline.shots_showing_children()
    if not found:
        return [], ""

    def plans() -> dict:
        return {s.shot_id: s for f in sorted(pipeline.p.shots.glob("*.json"))
                for s in ShotList.model_validate_json(f.read_text(encoding="utf-8")).shots}

    def key(shot) -> str:
        return hashlib.sha256(json.dumps([shot.characters_in_frame, shot.image_prompt, shot.motion_prompt],
                                         ensure_ascii=False).encode()).hexdigest()[:16]
    path = pipeline.p.reports / CHILD_STOPS
    try:
        stops = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stops = {}
    stops = stops if isinstance(stops, dict) else {}
    current, flagged = plans(), set(found)
    seen = {sid: stops[sid] for sid in found
            if isinstance(stops.get(sid), dict) and stops[sid].get("plan") == key(current[sid])}   # asked already
    stuck = [sid for sid, entry in seen.items() if not entry.get("goes_on")]
    kept_before = [sid for sid, entry in seen.items() if entry.get("goes_on")]
    todo, redone, again, once = [sid for sid in found if sid not in seen], [], {}, []
    for attempt in (1, 2):
        groups: dict[str, list[str]] = {}
        for sid in todo:                               # one call per note: the bucket is cleaned once per batch
            kind, _, who = found[sid]
            groups.setdefault(_child_note(kind, who, again.get(sid, "")), []).append(sid)
        for note, sids in groups.items():
            pipeline.rework_shots(sids, note, reason="Google's video model refuses to show a child in it, and it "
                                                     "has not been made yet.")
        redone += [sid for sid in todo if sid not in redone]
        now = pipeline.shots_showing_children() if todo else {}
        left = [sid for sid in todo if sid in now]
        found.update({sid: now[sid] for sid in left})
        if attempt == 1:                               # asked once more, except a child word (CHILD_ASK_ONCE)
            todo = [sid for sid in left if now[sid][0] not in CHILD_ASK_ONCE]
            once = [sid for sid in left if sid not in todo]
            again = {sid: now[sid][1] for sid in todo}
        else:
            todo = left
    left = todo + once
    kept = [sid for sid in left if found[sid][0] not in CHILD_STOP_KINDS]   # only named, or a child word: goes on
    stuck += [sid for sid in left if sid not in kept]
    current = plans()
    stops = {sid: entry for sid, entry in stops.items()      # records of shots this run did not look at: kept
             if sid not in flagged and isinstance(entry, dict) and sid in current
             and entry.get("plan") == key(current[sid])}
    stops.update({sid: {"plan": key(current[sid]), "why": found[sid][1], "goes_on": sid in kept or sid in kept_before}
                  for sid in [*stuck, *kept, *kept_before]})
    pipeline.p.reports.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stops, indent=1, ensure_ascii=False), encoding="utf-8")
    redone = [sid for sid in redone if sid not in stuck]
    if redone:
        log(f"  planned again without a child (Google refuses to show one): {', '.join(redone)}")
    for sid in kept:
        log(f"  {sid}: planned again and {found[sid][1][len('it '):]}; it goes on as planned")
    if not stuck:
        return redone, ""
    return redone, (f"{len(stuck)} shot(s) still show a child after the director planned them again: "
                    + "; ".join(f"{sid} ({found[sid][1]})" for sid in stuck)
                    + ". Send them back with a note on who or what should be in frame instead")


def approve(pipeline: Pipeline, gate: str, text: str = "approved by policy\n") -> None:
    pipeline.p.reviews.mkdir(parents=True, exist_ok=True)
    (pipeline.p.reviews / f"{gate}.pending").unlink(missing_ok=True)
    (pipeline.p.reviews / f"{gate}.approved").write_text(text, encoding="utf-8")


def approved(pipeline: Pipeline, gate: str) -> bool:
    return (pipeline.p.reviews / f"{gate}.approved").exists()


def approved_figure(pipeline: Pipeline, gate: str) -> float | None:
    """The estimate the owner approved: the JSON line the marker carried into the approval; None when none was."""
    try:
        for line in (pipeline.p.reviews / f"{gate}.approved").read_text(encoding="utf-8").splitlines():
            if line.startswith("{"):
                return float(json.loads(line).get("total_usd") or 0)
    except (OSError, ValueError):
        pass
    return None


def hold(pipeline: Pipeline, gate: str, text: str) -> None:
    """Leave the gate's pending marker, the same file a pipeline gate writes: the control room lists it, and an
    approval in the queue turns it into <gate>.approved before the next run."""
    if gate not in GATE_ORDER:                            # a marker no approve button or CLI choice could ever clear
        raise ValueError(f"unknown gate {gate!r}; the gates are {', '.join(GATE_ORDER)}")
    pipeline.p.reviews.mkdir(parents=True, exist_ok=True)
    (pipeline.p.reviews / f"{gate}.pending").write_text(text, encoding="utf-8")


def clear(pipeline: Pipeline, gate: str, *suffixes: str) -> None:
    """Drop a runner gate's markers: a pending one the run passed, an approval that was used up."""
    for suffix in suffixes or ("pending",):
        (pipeline.p.reviews / f"{gate}.{suffix}").unlink(missing_ok=True)


def _faces_needed(pipeline: Pipeline) -> bool:
    """The faces gate applies when video is paid from first frames and no clips exist yet, or when the video step
    held it again for stills drawn while it ran (faces.pending); an approval passes it."""
    cfg = pipeline.cfg
    if cfg.get("providers", {}).get("video") != "veo" or cfg.get("google", {}).get("veo_mode", "keyframe") != "keyframe":
        return False
    if (pipeline.p.reviews / "faces.approved").exists():
        return False
    if (pipeline.p.reviews / "faces.pending").exists():  # held mid-video for new stills: clips exist, it still waits
        return True
    return not any((pipeline.p.root / "inbox" / "video").glob("*/*.mp4"))


def _teed(log, progress):
    """The same log, each line also handed to the progress record as its last line."""
    def tee(text="", *args, **kwargs):
        log(text, *args, **kwargs)
        progress.line(str(text).strip())
    return tee


def produce(pipeline: Pipeline, *, bucket: str | None = None, publish: str | None = None, gates: str | None = None,
            keyframes=None, log=print, progress=None) -> dict:
    """Drive the project as far as its policy allows. `keyframes` is the first-frames step (the CLI's command),
    injected so tests can skip it. `progress` (a service.progress.FilmProgress) hears every stage, step and log
    line while the run works, so the control room can show where a running film is. Returns and writes the report."""
    root = pipeline.p.root
    if progress is not None:
        pipeline.on_stage = progress.phase
        pipeline.log, log = _teed(pipeline.log, progress), _teed(log, progress)
    step = progress.phase if progress is not None else (lambda name: None)
    pol = policy_of(pipeline.cfg, gates=gates, publish=publish)
    report = {"slug": root.name, "version": os.environ.get("NRAIORBIT_VERSION") or "unknown",
              "started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "policy": pol, "steps": {}, "state": "running"}

    if bucket and getattr(pipeline, "remove_remote", None) is None:   # a redone step's throwaways leave the bucket
        from ..ops import backup as _backup
        pipeline.remove_remote = lambda paths: _backup.remove_remote(root, bucket, paths, log=log)
    rework = apply_rework(pipeline, log)                  # the owner's Send back, before anything runs
    if rework:
        report["steps"]["rework"] = f"{rework['stage'] or 'nothing'}: {rework['what']}"
        if bucket:                                        # the page shows the decision now, not after the run
            from ..ops import backup as _backup
            from ..pipeline import OWNER_NOTES
            _backup.put_report(root, bucket, OWNER_NOTES, log=log)

    def finish(state: str) -> dict:
        report["state"] = state
        report["ledger_usd"] = round(SpendLedger(pipeline.p.reports / "cost.jsonl", None).spent(), 2)
        report["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        pipeline.p.reports.mkdir(parents=True, exist_ok=True)
        (pipeline.p.reports / "produce.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        from .rehearse import save as rehearse_save           # what stands ahead now, for the control room
        rehearse_save(pipeline, log=log)
        log(f"  produce: {state}")
        if progress is not None:
            progress.finish(state)
        return report

    # 1. plan up to the shot lists (Claude only), honouring the story and cast gates
    while True:
        try:
            pipeline.run(until="shots")
            break
        except GatePending as g:
            if pol["gates"] == "auto" and g.gate not in OWNER_GATES:
                approve(pipeline, g.gate)
                continue
            report["steps"]["plan"] = f"awaiting:{g.gate}"
            return finish(f"awaiting:{g.gate}")
        except (PipelineBlocked, BudgetExceeded) as b:
            report["steps"]["plan"] = f"blocked: {b}"
            return finish(f"blocked:{str(b)[:120]}")
    report["steps"]["plan"] = "done"

    # a planned shot Google would refuse for showing a child is planned again now, before anything is paid or sent
    redone, stop = replan_child_shots(pipeline, log)
    if redone:
        report["steps"]["children"] = f"planned again without a child: {', '.join(redone)}"
    if stop:
        report["steps"]["children"] = f"blocked: {stop}"
        return finish(f"blocked:{stop[:160]}")

    # a film that cannot be voiced is never offered a cost to approve: speech runs after each scene's clips
    step("voices")                                       # its stop belongs on the voices step, not the shot lists
    voiceless = pipeline._voiceless()
    if voiceless:
        why = (f"voices: no ElevenLabs voice for {voiceless}; add voice.cast.<character>: <voice id> to the "
               "project's pipeline.yaml (or elevenlabs_voice to the character's library voice), then retry")
        report["steps"]["render"] = f"blocked: {why}"
        return finish(f"blocked:{why[:160]}")

    # 2. the cost gate: what the paid stages (picture, voices, music, lip-sync) would cost against what is left of the
    #    cap, before any of them is paid. Over the cap it blocks; under it the owner approves the figure, under every
    #    policy. Only planning and the cast's reference pictures come before it.
    step("cost")
    block, preview = cost_gate(pipeline)
    if preview:
        report["steps"]["estimate"] = {k: preview[k] for k in ("total_usd", "video_usd", "keyframe_usd", "tts_usd",
                                                              "music_usd", "lipsync_usd") if k in preview}
    if block:
        report["steps"]["render"] = block["why"]
        return finish(block["state"])
    total = float(preview.get("total_usd") or 0) if preview else 0.0
    if total > 0:
        was = approved_figure(pipeline, "cost") if approved(pipeline, "cost") else None
        grown = was is not None and total > was * (1 + COST_GROWTH) + 0.5     # the plan changed since the approval
        if not approved(pipeline, "cost") or grown:
            marker = json.dumps(report["steps"]["estimate"]) + "\n"
            clear(pipeline, "cost", "approved")
            hold(pipeline, "cost", marker)
            report["steps"]["cost"] = "awaiting:cost" + (f" (the estimate grew from {was:.2f} to {total:.2f})"
                                                         if grown else "")
            return finish("awaiting:cost")
    clear(pipeline, "cost")                                   # passed: never shown as waiting
    report["steps"]["cost"] = "approved" if approved(pipeline, "cost") else "not needed"

    # 3. faces: first frames for every shot with a lead, then a human look (ask) or straight on (auto)
    step("faces")
    if _faces_needed(pipeline) and pol["gates"] == "ask":
        if keyframes is not None:
            try:
                keyframes(SimpleNamespace(slug=root.name, only=None, max_spend=None, yes=True))
            except SystemExit as e:                      # the command reports its own reason
                report["steps"]["faces"] = f"blocked: {e}"
                return finish(f"blocked:{str(e)[:120]}")
        if not (pipeline.p.reviews / "faces.pending").exists():   # a hold naming new stills keeps its words
            hold(pipeline, "faces", "Look at the first frames in the control room, then approve faces.\n")
        report["steps"]["faces"] = "awaiting:faces"
        return finish("awaiting:faces")
    clear(pipeline, "faces")
    report["steps"]["faces"] = "approved" if approved(pipeline, "faces") else "not needed"

    # 4. the paid stages, assembly and trailer under the cap
    pipeline.look_at_new_frames = pol["gates"] == "ask"   # a still drawn now waits for the faces gate before its clip
    while True:
        try:
            pipeline.run()
            break
        except GatePending as g:
            if pol["gates"] == "auto" and g.gate not in OWNER_GATES:
                approve(pipeline, g.gate)
                continue
            report["steps"]["render"] = f"awaiting:{g.gate}"
            return finish(f"awaiting:{g.gate}")
        except (PipelineBlocked, BudgetExceeded) as b:
            from ..providers.google_ai import quota_used_up
            if not isinstance(b, BudgetExceeded) and quota_used_up(b):   # what was made is kept: the next tick
                report["steps"]["render"] = f"quota: {str(b)[:160]}"     # goes on from there (the runner counts)
                return finish("retry:quota")
            report["steps"]["render"] = f"blocked: {b}"
            return finish(f"blocked:{str(b)[:120]}")
    report["steps"]["render"] = "done"

    # 4. QA: never stops the run; it writes sheets and a verdict for the owner
    step("qa")
    qa_file, checked = pipeline.p.reports / "qa" / "film.json", False   # what `nraiorbit qa film` writes: the control
    try:                                                                # room's QA card reads it, newest first
        from ..qa import film
        fc = film.check(root, log=log)
        report["steps"]["qa_film"] = "ok" if fc.get("ok") else "look"
        qa_file.parent.mkdir(parents=True, exist_ok=True)
        qa_file.write_text(json.dumps(fc, indent=1, default=str), encoding="utf-8")
        checked = True
        if pipeline.cfg.get("providers", {}).get("lipsync", "none") != "none":
            from ..qa import lipsync
            rows = lipsync.review(root, log=log)
            report["steps"]["qa_lipsync"] = f"{sum(1 for r in rows if r.get('flags'))} of {len(rows)} shots flagged"
    except Exception as exc:                              # missing mediapipe, no speaking shots: QA is advisory
        report["steps"]["qa"] = f"skipped: {type(exc).__name__}: {str(exc)[:100]}"
        if not checked:                                   # a skipped check replaces the last cut's verdict, never shows it
            try:
                qa_file.parent.mkdir(parents=True, exist_ok=True)
                qa_file.write_text(json.dumps({"ok": None, "skipped": report["steps"]["qa"][len("skipped: "):]}),
                                   encoding="utf-8")
            except OSError:
                pass

    # 5. deliverables and links
    step("deliver")
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

    # 6. YouTube, private unless the policy says otherwise; approved by the owner first under every policy; never
    #    a browser
    step("publish")
    if pol["publish"] not in ("private", "unlisted", "public"):
        clear(pipeline, "publish")
        report["steps"]["publish"] = "none"
    elif not approved(pipeline, "publish"):
        qa = "" if report["steps"].get("qa_film") == "ok" else " The film check did not pass: look at QA first."
        hold(pipeline, "publish", f"Watch the film and the trailer, then approve the {pol['publish']} YouTube "
                                  f"upload.{qa}\n")
        report["steps"]["publish"] = "awaiting:publish"
        return finish("awaiting:publish")
    else:
        try:
            report["steps"]["publish"] = publish_youtube(root, pol["publish"])
            clear(pipeline, "publish", "pending", "approved")     # one approval, one upload: a new cut asks again
        except Exception as exc:
            report["steps"]["publish"] = f"skipped: {type(exc).__name__}: {str(exc)[:160]}"

    # 7. backup
    if bucket:
        step("backup")
        try:
            from ..ops import backup
            report["steps"]["backup"] = f"{backup.backup(root, bucket, log=log)} sets"
        except Exception as exc:
            report["steps"]["backup"] = f"failed: {type(exc).__name__}: {str(exc)[:160]}"
    return finish("done")


def cost_gate(pipeline: Pipeline) -> tuple[dict | None, dict | None]:
    """(a block to report, the preview) before the paid stages. A block when the paid generators' estimate plus
    what is spent would pass the cap, or when there is an estimate and no cap at all. Cached work costs nothing
    and never blocks."""
    from ..costing import preview_run

    cfg, prov = pipeline.cfg, pipeline.cfg.get("providers", {})
    voice, ls = cfg.get("voice", {}), cfg.get("lipsync") or {}
    paid = (prov.get("video") == "veo" or prov.get("tts") in ("gemini", "elevenlabs") or prov.get("music") == "lyria"
            or (prov.get("tts") == "inbox" and voice.get("inbox_fallback") in ("gemini", "elevenlabs"))
            or (prov.get("lipsync") == "sync_so" and not ls.get("offline")))
    if not paid or not (pipeline.p.root / "04_shots").exists():
        return None, None
    preview = preview_run(pipeline.p.root, cfg, pipeline.pricing)
    estimate = float(preview.get("total_usd") or 0.0)
    if estimate <= 0:
        return None, preview
    spent = pipeline.ledger.spent()
    cap = (cfg.get("budget") or {}).get("max_usd")
    if cap is None or cap <= 0:
        return {"state": "blocked:no cap", "why": f"the paid stages would cost about ${estimate:.2f} and "
                                                   "budget.max_usd is not set; set it (or --max-spend) first"}, preview
    if spent + estimate > cap:
        return {"state": "blocked:estimate", "why": f"estimate ${estimate:.2f} + spent ${spent:.2f} is over the cap "
                                                     f"${cap:.2f}; raise the cap or reduce the plan"}, preview
    return None, preview


def publish_youtube(root: Path, privacy: str) -> dict:
    """Upload the film (and the trailer when there is one) with the metadata the outline gives. The OAuth client
    secrets come from YOUTUBE_CLIENT_SECRETS (default client_secret.json next to the project) and the token, made
    once on a laptop, from YOUTUBE_TOKEN (default <project>/.youtube_token.json)."""
    from ..publish import youtube
    from ..schemas import Outline, StoryBible

    secrets = Path(os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secret.json"))
    token = Path(os.environ.get("YOUTUBE_TOKEN", str(root / ".youtube_token.json")))
    if not secrets.exists() and not youtube.token_available(token):
        raise FileNotFoundError("no YouTube token: press Connect YouTube in the control room")
    bible = StoryBible.model_validate_json((root / "01_story_bible.json").read_text(encoding="utf-8"))
    outline = Outline.model_validate_json((root / "02_outline.json").read_text(encoding="utf-8"))
    creds = youtube.credentials(secrets, token, interactive=False)
    master = root / "11_master"
    done_path = root / "reports" / "youtube.json"           # what was uploaded, per file: the same file never twice
    try:
        out = json.loads(done_path.read_text(encoding="utf-8")) if done_path.exists() else {}
    except ValueError:
        out = {}

    def record():
        done_path.parent.mkdir(parents=True, exist_ok=True)
        done_path.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")

    meta = youtube.build_metadata(bible, outline, privacy=privacy)
    captions = {lang: master / f"subs_{lang}.srt" for lang in ("te", "en") if (master / f"subs_{lang}.srt").exists()}
    thumb = master / "thumbnail.png"
    if not uploaded_already(out, "film", master / "movie.mp4"):
        video_id = youtube.upload(master / "movie.mp4", meta, creds, captions=captions,
                                  thumbnail=thumb if thumb.exists() else None)
        out["film"] = {"id": video_id, "file": fingerprint(master / "movie.mp4")}
        record()
    if (master / "trailer.mp4").exists() and not uploaded_already(out, "trailer", master / "trailer.mp4"):
        tmeta = youtube.build_metadata(bible, outline, privacy=privacy)
        tmeta["snippet"]["title"] = f"{bible.title_te} ({bible.title_en}) | Official Trailer"[:100]
        out["trailer"] = {"id": youtube.upload(master / "trailer.mp4", tmeta, creds),
                          "file": fingerprint(master / "trailer.mp4")}
        record()
    return out


def fingerprint(path: Path) -> dict:
    """What identifies a cut: its size and content hash, so a re-cut (even one restored over an old record) is new."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": h.hexdigest()}


def uploaded_already(record: dict, asset: str, path: Path) -> bool:
    """True when the record says this very file (same size and hash) went up; an old record of another cut does not."""
    rec = record.get(asset)
    return isinstance(rec, dict) and rec.get("file") == fingerprint(path)
