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
from ..pipeline import CutDoesNotMatch, GatePending, LinesTooLong, Pipeline, PipelineBlocked
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


DIALOGUE_FIT = "dialogue_fit.json"         # reports/: lines shortened to fit their shots, by block id
MIN_LINE_S = 1.0                           # no line is asked to be shorter than this


def _measured_lines(pipeline: Pipeline, scripts: dict) -> dict[str, float]:
    """The voiced length of every line whose words are still the ones voiced: from the cut (reports/edl.json, which
    notes each line's words); a line this step shortened since is estimated until it is voiced again."""
    from ..planning.fit import text_sha as _text_sha
    try:
        edl = json.loads((pipeline.p.reports / "edl.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    try:
        changed = json.loads((pipeline.p.reports / DIALOGUE_FIT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        changed = {}
    changed = changed if isinstance(changed, dict) else {}
    texts = {b.block_id: b.text_te for script in scripts.values() for b in script.blocks}
    out = {}
    for d in edl.get("dialogue") or []:
        bid = d.get("ref_id")
        if bid not in texts:
            continue
        sha = d.get("text_sha")
        if (sha and sha != _text_sha(texts[bid])) or (not sha and bid in changed):
            continue                                       # its words changed since it was voiced
        out[bid] = float(d.get("duration_s") or 0)
    return out


FIT_TRIES = 2                              # times one line may be rewritten before the run stops and says so


def fit_dialogue(pipeline: Pipeline, log=print, measured: dict[str, float] | None = None) -> list[str]:
    """Lines that would run past the shots carrying them (planning/fit) are written shorter by the screenwriter, so
    every voice plays over its own picture: lengths as voiced (the render step that just stopped, or the last cut),
    else estimated. The first line that overruns and the lines that pushed it late share their room; a line with no
    room left (MIN_LINE_S) is dropped. Every scene is asked before anything is written; a changed line's old voice
    files go, so it is voiced again, and the cut is made again from the clips already paid for. A line is rewritten
    at most FIT_TRIES times, never when a voice artist recorded it (inbox/dialogue), and a rewrite counts only when
    its words change and fit the length asked. Returns the lines changed ([] when everything fits, nothing more can
    be done, or the film is not planned yet)."""
    import shutil
    from ..planning.fit import DIALOGUE_GAP_S, OVERRUN_TOL_S, chars_for, plan_lines, speech_seconds
    from ..planning.runtime import scene_start_times
    from ..providers.inbox import _find_audio
    from ..schemas import Outline, SceneScript, ShotList, StoryBible
    from ..shotpack import inbox_dir
    p = pipeline.p
    if not p.outline.exists():
        return []
    outline = Outline.model_validate_json(p.outline.read_text(encoding="utf-8"))
    starts, scenes, scripts = scene_start_times(outline), [], {}
    for sc in outline.scenes:
        sp, pp = p.scenes / f"{sc.scene_id}.json", p.shots / f"{sc.scene_id}.json"
        if not (sp.exists() and pp.exists()):
            return []                                       # still being planned: the planning checks see to it
        scripts[sc.scene_id] = SceneScript.model_validate_json(sp.read_text(encoding="utf-8"))
        shots = ShotList.model_validate_json(pp.read_text(encoding="utf-8")).shots
        scenes.append((sc.scene_id, starts[sc.scene_id], shots, scripts[sc.scene_id].blocks))
    film_end = float(sum(sc.target_seconds for sc in outline.scenes))
    durations = {**_measured_lines(pipeline, scripts), **(measured or {})}
    try:
        record = json.loads((p.reports / DIALOGUE_FIT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    record = record if isinstance(record, dict) else {}
    recorded = inbox_dir(p.root, "dialogue")
    fixed = {bid for bid, r in record.items() if isinstance(r, dict) and int(r.get("tries") or 0) >= FIT_TRIES}
    fixed |= {b.block_id for sc in scripts.values() for b in sc.blocks
              if b.kind == "dialogue" and _find_audio(recorded, b.block_id) is not None}   # a voice artist's take
    targets: dict[str, float] = {}
    for _ in range(200):                     # the first line that overruns, and the lines that pushed it late
        lines = plan_lines(scenes, {**durations, **targets}, film_end)
        first = next((i for i, ln in enumerate(lines) if ln.overrun_s(film_end) > 0), None)
        if first is None:
            break
        j = first                            # back to the line that starts on time: the chain shares the room
        while j > 0 and lines[j].scene_id == lines[j - 1].scene_id and lines[j].start_s > lines[j].earliest_s + 1e-3:
            j -= 1
        chain = lines[j:first + 1]
        room = min(lines[first].window_end_s + OVERRUN_TOL_S, film_end) - chain[0].earliest_s \
            - DIALOGUE_GAP_S * (len(chain) - 1)
        total = sum(ln.dur_s for ln in chain if ln.block_id not in fixed) or 1.0
        share = max(0.0, room - sum(ln.dur_s for ln in chain if ln.block_id in fixed)) / total
        moved = False
        for ln in chain:
            if ln.block_id in fixed:
                continue
            want = round(ln.dur_s * min(share, 0.97), 2)
            if want < MIN_LINE_S:
                want = 0.0                   # no room for it: dropped rather than cut to one word
            if want < ln.dur_s - 1e-3:
                targets[ln.block_id] = want
                moved = True
        if not moved:
            break                            # nothing left that may change: the checks stop the run and say so
    blocks = {b.block_id: (sid, b) for sid, script in scripts.items() for b in script.blocks}
    shrink = {bid: t for bid, t in targets.items()
              if t < (durations.get(bid) or speech_seconds(blocks[bid][1].text_te)) - 0.05}
    if not shrink:
        return []
    bible = StoryBible.model_validate_json(p.bible.read_text(encoding="utf-8"))
    answers: dict[str, dict[str, tuple[str, str]]] = {}
    for sid in sorted({blocks[bid][0] for bid in shrink}):   # every scene asked before anything is written
        want = {bid: t for bid, t in shrink.items() if blocks[bid][0] == sid}
        got = {bid: ("", "") for bid, t in want.items() if t == 0.0}    # no room: dropped
        for attempt in (1, 2):                              # a line still too long is asked for once more
            ask = {bid: chars_for(t if attempt == 1 else t * 0.85) for bid, t in want.items() if bid not in got}
            if not ask:
                break
            try:
                out = pipeline.screenwriter.shorten_lines(bible, outline, scripts[sid], ask)
            except Exception as exc:                        # this scene stays as it is; the checks stop it later
                log(f"  {sid}: the screenwriter could not shorten its lines ({type(exc).__name__}: {str(exc)[:120]})")
                break
            for line in out.lines:
                te = line.text_te.strip()
                if line.block_id in ask and te != blocks[line.block_id][1].text_te.strip() and \
                        speech_seconds(te) <= want[line.block_id]:
                    got[line.block_id] = (te, line.text_en.strip())        # "" drops the line
        if got:
            answers[sid] = got
    done = []
    for sid, got in answers.items():
        new_blocks = [b.model_copy(update={"text_te": got[b.block_id][0], "text_en": got[b.block_id][1]})
                      if b.block_id in got else b for b in scripts[sid].blocks
                      if not (b.block_id in got and not got[b.block_id][0])]     # a dropped line leaves the scene
        p.scenes.joinpath(f"{sid}.json").write_text(scripts[sid].model_copy(update={"blocks": new_blocks})
                                                    .model_dump_json(indent=2), encoding="utf-8")
        for bid, (te, _) in got.items():
            shutil.rmtree(p.dialogue / bid, ignore_errors=True)   # voiced again with the new words
            was = record.get(bid) if isinstance(record.get(bid), dict) else {}
            record[bid] = {"was_te": was.get("was_te", blocks[bid][1].text_te), "now_te": te,
                           "keep_to_s": round(shrink[bid], 2), "tries": int(was.get("tries") or 0) + 1,
                           "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
            done.append(bid)
    for bid in shrink:                                       # asked but not rewritten: that counts as a try too
        if bid not in done:
            was = record.get(bid) if isinstance(record.get(bid), dict) else {}
            record[bid] = {**was, "was_te": was.get("was_te", blocks[bid][1].text_te),
                           "tries": int(was.get("tries") or 0) + 1}
    p.reports.mkdir(parents=True, exist_ok=True)
    (p.reports / DIALOGUE_FIT).write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    if not done:
        return []
    (p.edit / "timeline.json").unlink(missing_ok=True)     # the cut is made again with the new voices
    pipeline.invalidate("assemble")
    kept = [b for b in done if record[b]["now_te"]]
    dropped = [b for b in done if not record[b]["now_te"]]
    log("  to fit their shots: " + "; ".join(x for x in (f"shortened {', '.join(kept)}" if kept else "",
                                                         f"dropped {', '.join(dropped)}" if dropped else "") if x))
    return done


FACE_REPLANS = "face_replans.json"          # reports/: shots planned again as close-ups because no face was found
CLOSE_UP_NOTE = ("Lip-sync could not find the speaker's face in this shot. Plan it again as a medium close-up of "
                 "whoever speaks here (both speakers in a tight two-shot if two of them talk): size MCU or CU, their "
                 "faces large in frame, turned toward the camera at a slight angle, clearly lit, mouths visible. Keep "
                 "the story moment and the place; the other characters may be out of frame or soft behind them.")


def replan_faceless(pipeline: Pipeline, shots: list[str], log=print) -> list[str]:
    """Shots whose speaker lip-sync could not find (too small, turned away) planned again by the director as close-ups,
    once each (reports/face_replans.json): a fresh take, so new first frames are drawn and, under "ask", the faces
    check shows them. Returns the shots planned again ([] when each was tried before: the cut stays stopped)."""
    path = pipeline.p.reports / FACE_REPLANS
    try:
        done = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = {}
    done = done if isinstance(done, dict) else {}
    todo = [sid for sid in shots if sid not in done]
    if not todo:
        return []
    found = pipeline.rework_shots(todo, CLOSE_UP_NOTE, reason="lip-sync found no speaker's face in it.")
    done.update({sid: datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds") for sid in found})
    pipeline.p.reports.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(done, indent=1), encoding="utf-8")
    if found:
        log(f"  planned again as close-ups (lip-sync found no speaker's face): {', '.join(found)}")
    return found


def spent_usd(pipeline: Pipeline) -> float:
    return round(SpendLedger(pipeline.p.reports / "cost.jsonl", None).spent(), 2)


def approved_spent(pipeline: Pipeline, gate: str) -> float | None:
    """What had been spent when the estimate the owner approved was made (its marker's spent_usd); None for an
    approval from before that was written down."""
    try:
        for line in (pipeline.p.reviews / f"{gate}.approved").read_text(encoding="utf-8").splitlines():
            if line.startswith("{"):
                at = json.loads(line).get("spent_usd")
                return None if at is None else float(at)
    except (OSError, ValueError, TypeError):
        pass
    return None


def within_approval(pipeline: Pipeline, total: float) -> bool | None:
    """Whether what was spent since the owner approved the cost, plus `total` still to pay, stays within the approved
    figure (COST_GROWTH): an estimate counts only what is left to make, so re-planned shots added after most of the
    film was paid for would otherwise pass under an approval for the whole film. None when the approval does not say
    what had been spent (an older one)."""
    was, at = approved_figure(pipeline, "cost"), approved_spent(pipeline, "cost")
    if was is None or at is None:
        return None
    return (spent_usd(pipeline) - at) + total <= was * (1 + COST_GROWTH) + 0.5


def cost_marker(pipeline: Pipeline, estimate: dict) -> str:
    """The cost gate's marker: the estimate, and what had been spent when it was made."""
    return json.dumps({**estimate, "spent_usd": spent_usd(pipeline)}) + "\n"


def _cost_again(pipeline: Pipeline, report: dict, why: str) -> str | None:
    """After the plan changed mid-run (lines shortened, shots planned again): None when it costs nothing more, or
    when what was spent since the owner's approval plus the new estimate stays within it (within_approval), so the
    run goes straight on; else the state to stop in (over the cap, or the cost asked again with the new figure). An
    approval that does not say what had been spent is asked again for any cost."""
    block, preview = cost_gate(pipeline)
    estimate = {k: preview[k] for k in ("total_usd", "video_usd", "keyframe_usd", "tts_usd", "music_usd",
                                        "lipsync_usd") if k in preview} if preview else {}
    report["steps"]["estimate"] = estimate
    if block:
        report["steps"]["render"] = block["why"]
        return block["state"]
    total = float(estimate.get("total_usd") or 0)
    if not total or (approved(pipeline, "cost") and within_approval(pipeline, total)):
        return None
    clear(pipeline, "cost", "approved")
    hold(pipeline, "cost", cost_marker(pipeline, estimate))
    report["steps"]["cost"] = f"awaiting:cost ({why})"
    return "awaiting:cost"


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
        if hasattr(progress, "fence"):                   # a run that lost touch with the bucket pays for nothing more
            pipeline.ledger.fence = progress.fence
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

    # lines that would run past their shots are written shorter now, before anything is paid for them
    shortened = fit_dialogue(pipeline, log)
    if shortened:                                        # their new voices: the cost check below asks again only
        report["steps"]["dialogue"] = f"shortened to fit their shots: {', '.join(shortened)}"   # if the bill grew

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
        fits = within_approval(pipeline, total) if was is not None else None
        grown = was is not None and (not fits if fits is not None else      # spent since, plus what is left
                                     total > was * (1 + COST_GROWTH) + 0.5)  # an older approval: the estimate alone
        if not approved(pipeline, "cost") or grown:
            clear(pipeline, "cost", "approved")
            hold(pipeline, "cost", cost_marker(pipeline, report["steps"]["estimate"]))
            report["steps"]["cost"] = "awaiting:cost" + (f" (the cost grew past the ${was:.2f} approved)" if grown
                                                         else "")
            return finish("awaiting:cost")
    clear(pipeline, "cost")                                   # passed: never shown as waiting
    report["steps"]["cost"] = "approved" if approved(pipeline, "cost") else "not needed"

    # 3. faces: first frames for every shot with a lead, then a human look (ask) or straight on (auto)
    step("faces")
    if _faces_needed(pipeline) and pol["gates"] == "ask":
        if keyframes is not None:
            try:
                keyframes(SimpleNamespace(slug=root.name, only=None, max_spend=None, yes=True, ledger=pipeline.ledger))
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
        except CutDoesNotMatch as off:                     # no face for lip-sync: those shots planned closer, once
            again = replan_faceless(pipeline, off.faceless, log) if off.faceless and not off.timing else []
            if not again:
                report["steps"]["render"] = f"blocked: {off}"
                return finish(f"blocked:{str(off)[:120]}")
            report["steps"]["close_ups"] = f"planned again as close-ups: {', '.join(again)}"
            stop = _cost_again(pipeline, report, "shots planned again as close-ups")
            if stop is None:
                continue                                  # new first frames: the faces check stops it under "ask"
            return finish(stop)
        except LinesTooLong as long_lines:                # voiced lines past their shots: shortened, cost asked
            shortened = fit_dialogue(pipeline, log, measured=long_lines.measured)
            if not shortened:
                report["steps"]["render"] = f"blocked: {long_lines}"
                return finish(f"blocked:{str(long_lines)[:120]}")
            report["steps"]["dialogue"] = f"shortened to fit their shots: {', '.join(shortened)}"
            stop = _cost_again(pipeline, report, "lines shortened to fit their shots")
            if stop is None:
                continue                                  # within what the owner approved: straight on
            return finish(stop)
        except (PipelineBlocked, BudgetExceeded) as b:
            from ..providers.google_ai import quota_used_up
            if not isinstance(b, BudgetExceeded) and quota_used_up(b):   # what was made is kept: the next tick
                report["steps"]["render"] = f"quota: {str(b)[:160]}"     # goes on from there (the runner counts)
                return finish("retry:quota")
            report["steps"]["render"] = f"blocked: {b}"
            return finish(f"blocked:{str(b)[:120]}")
    report["steps"]["render"] = "done"

    # 3b. reels: 5-second upright clips cut from the film itself for Instagram and YouTube Shorts; nothing is paid,
    #     and a reel that cannot be made never stops the film
    if (pipeline.cfg.get("reels") or {}).get("enabled", True):
        step("reels")
        try:
            from ..post import reels
            r = pipeline.cfg.get("reels") or {}
            report["steps"]["reels"] = reels.make_reels(
                root, count=int(r.get("count", reels.REEL_COUNT)), length=float(r.get("seconds", reels.REEL_S)),
                fps=int((pipeline.cfg.get("film") or {}).get("fps", 24)), log=log)
        except Exception as exc:
            report["steps"]["reels"] = f"skipped: {type(exc).__name__}: {str(exc)[:160]}"

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
    elif not approved(pipeline, "publish") and published_already(root):
        report["steps"]["publish"] = json.loads((root / "reports" / "youtube.json").read_text(encoding="utf-8"))
        clear(pipeline, "publish")                       # this very film and trailer are up: never asked again
    elif not approved(pipeline, "publish"):
        qa = "" if report["steps"].get("qa_film") == "ok" else " The film check did not pass: look at QA first."
        hold(pipeline, "publish", f"Watch the film and the trailer, then approve the {pol['publish']} YouTube "
                                  f"upload.{qa}\n")
        report["steps"]["publish"] = "awaiting:publish"
        return finish("awaiting:publish")
    else:
        if pipeline.ledger.fence is not None:            # out of touch: another run may be uploading it now
            pipeline.ledger.fence()
        try:
            report["steps"]["publish"] = publish_youtube(root, pol["publish"], bucket=bucket)
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


def publish_youtube(root: Path, privacy: str, bucket: str | None = None) -> dict:
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
        if bucket:                                        # in the bucket at once: a run killed now never uploads it again
            from ..ops import backup
            backup.put_report(root, bucket, done_path.name)

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


def published_already(root: Path) -> bool:
    """Whether this very film (and its trailer, when there is one) is on YouTube already (reports/youtube.json): a
    run over a finished film (to make its reels, say) does not ask for the upload again. A new cut is asked for."""
    master = root / "11_master"
    try:
        record = json.loads((root / "reports" / "youtube.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict) or not (master / "movie.mp4").exists():
        return False
    return uploaded_already(record, "film", master / "movie.mp4") and (
        not (master / "trailer.mp4").exists() or uploaded_already(record, "trailer", master / "trailer.mp4"))


def uploaded_already(record: dict, asset: str, path: Path) -> bool:
    """True when the record says this very file (same size and hash) went up; an old record of another cut does not."""
    rec = record.get(asset)
    return isinstance(rec, dict) and rec.get("file") == fingerprint(path)
