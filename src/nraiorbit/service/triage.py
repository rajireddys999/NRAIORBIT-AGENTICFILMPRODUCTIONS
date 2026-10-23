"""The owner rejected the cut or the trailer with a note: the editor reads the note against a compact summary of the
film, decides what to redo (the narrowest fix first), and the decision is applied with the steps that already exist.
Runs in the runner (it has the Claude key and the film's files), from produce.apply_rework, before anything runs.

Routes and what each redoes:
    mix            reports/owner_mix.json (music level, lines over music), then the cut is mastered again: free
    trailer        reports/trailer_direction.json from the editor, then the trailer is cut again: cents
    reshoot_shots  Pipeline.rework_shots (new words, a fresh take), the cost asked again: about a dollar a shot
    music          the score is planned again (MusicDirector follows the notes)
    shots … story  that step and every one after it (the Send back path)

Every paid step it reopens asks the cost check again, except a new trailer score (cents), which a re-cut trailer
pays like a Send back to the trailer does. A rejection is applied only while the film still waits at the review it
was made at (or is still finished): one the film has moved on from is recorded as not applied, never applied later
by surprise.
"""

from __future__ import annotations

import json

from ..pipeline import EDL, OWNER_MIX, STAGES, TRAILER_DIRECTION, TRAILER_PLAN
from ..schemas import MixChange, Outline, SceneScript, ShotList, StoryBible, TriageAction

ROUTE_STAGE = {"reshoot_shots": "render", "trailer": "trailer", "mix": "assemble", "music": "music_plan",
               "shots": "shots", "scenes": "scenes", "cast": "cast", "outline": "outline", "story": "story_bible"}
CUT_GATES = ("pacing", "final_cut")            # no trailer exists yet at these
MAX_SHOTS = 12
MAX_ACTIONS = 3
NO_ACTION = ("I could not tell what to change from that. Say which part (a time like 12:40, a shot id like "
             "sc002_s001, or the line) and what is wrong with it.")


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _rows(path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def film_map(pipeline) -> tuple[list[dict], list[dict]]:
    """(shots, lines) with their film times: from the cut's timeline when this container has it, else from
    reports/edl.json (kept in the bucket), else from the outline's scene starts and the shots' lengths."""
    p = pipeline.p
    cut = _read(p.edit / "timeline.json") or _read(p.reports / EDL) or {}
    outline = Outline.model_validate_json(p.outline.read_text(encoding="utf-8"))
    from ..planning.runtime import scene_start_times
    starts = scene_start_times(outline)
    times = {s["shot_id"]: s for s in cut.get("shots", [])}
    line_at = {d["ref_id"]: d for d in cut.get("dialogue", [])}
    shots, lines = [], []
    for sc in outline.scenes:
        path = p.shots / f"{sc.scene_id}.json"
        if not path.exists():
            continue
        t = starts[sc.scene_id]
        scene_shots = []
        for shot in ShotList.model_validate_json(path.read_text(encoding="utf-8")).shots:
            at = times.get(shot.shot_id, {})
            scene_shots.append({"shot_id": shot.shot_id, "scene_id": sc.scene_id,
                                "start_s": float(at.get("start_s", t)),
                                "duration_s": float(at.get("duration_s", shot.duration_s)),
                                "in_frame": shot.characters_in_frame, "size": shot.size, "rasa": shot.rasa,
                                "real": bool(at.get("real", True)), "image": shot.image_prompt[:100],
                                "block_ids": shot.block_ids})
            t += shot.duration_s
        shots += scene_shots
        script_path = p.scenes / f"{sc.scene_id}.json"
        if not script_path.exists():
            continue
        for b in SceneScript.model_validate_json(script_path.read_text(encoding="utf-8")).blocks:
            if b.kind != "dialogue":
                continue
            placed = line_at.get(b.block_id)
            if placed is None:                         # no cut: the start of the first shot that covers it
                first = next((s for s in scene_shots if b.block_id in s["block_ids"]), None)
                placed = {"start_s": first["start_s"] if first else starts[sc.scene_id]}
            lines.append({"line_id": b.block_id, "speaker": b.character_id, "start_s": float(placed["start_s"]),
                          "text": b.text_en[:160]})
    return shots, lines


def _trailer_rows(pipeline, with_times: bool) -> list[str]:
    plan = _read(pipeline.p.reports / TRAILER_PLAN) or _read(pipeline.p.edit / "trailer_plan.json") or []
    return [f"{c.get('section')}: {c.get('shot_id') or 'title card'}"
            + (f" at film {_mmss(c.get('film_start_s') or 0)}" if with_times and c.get("shot_id") else "")
            + (f", line {c['line_id']}" if c.get("line_id") else "") for c in plan if isinstance(c, dict)]


def moved_on(pipeline, gate: str) -> str:
    """Why a rejection made at `gate` no longer applies (the film is not at that review any more, say approved there
    since), or "" while it does."""
    if gate == "done":
        state = (_read(pipeline.p.reports / "produce.json", {}) or {}).get("state")
        return "" if state == "done" else f"the film is no longer finished ({state or 'never run'})"
    if (pipeline.p.reviews / f"{gate}.pending").exists():
        return ""
    return f"the film had moved on from the {gate.replace('_', ' ')} review"


def context(pipeline, gate: str) -> str:
    """A compact summary of the film for the editor: who is in it, every shot and line with its film time, the
    trailer as cut, what the review queue flagged, and the money."""
    p = pipeline.p
    bible = StoryBible.model_validate_json(p.bible.read_text(encoding="utf-8"))
    shots, lines = film_map(pipeline)
    people = [f"{c.id}: {c.name_en}, {c.role}, age {c.physical.age}" + (" (an animal)" if c.silent_animal else "")
              for c in bible.characters]
    shot_rows = [f"{s['shot_id']} {_mmss(s['start_s'])} {s['duration_s']:.0f}s {s['size']} {s['rasa']} "
                 f"[{', '.join(s['in_frame']) or 'nobody'}]{'' if s['real'] else ' NO VIDEO'}: {s['image']}"
                 for s in shots]
    line_rows = [f"{ln['line_id']} {_mmss(ln['start_s'])} {ln['speaker']}: {ln['text']}" for ln in lines]
    queue = [f"{r.get('shot_id')}: {'; '.join(map(str, r.get('reason') or []))[:140]}"
             for r in _rows(p.reports / "review_queue.jsonl")[-20:] if isinstance(r, dict)]
    from ..costing import SpendLedger
    spent = SpendLedger(p.reports / "cost.jsonl", None).spent()
    cap = (pipeline.cfg.get("budget") or {}).get("max_usd")
    parts = [
        f"Film: {bible.title_en} ({bible.title_te}). {bible.logline}",
        f"Review where the owner rejected it: {gate}" + (" (no trailer exists yet)" if gate in CUT_GATES else ""),
        "Characters:\n" + "\n".join(people),
        "Shots (id, start, length, size, mood, who is in frame, picture):\n" + "\n".join(shot_rows),
        "Dialogue lines (id, time, speaker, English):\n" + "\n".join(line_rows),
    ]
    trailer = _trailer_rows(pipeline, with_times=True)
    if trailer and gate not in CUT_GATES:
        parts.append("Trailer as cut (section: shot, film time, line):\n" + "\n".join(trailer))
    if queue:
        parts.append("Review queue (shots the checks flagged):\n" + "\n".join(queue))
    gain, floor = pipeline._owner_mix()
    floor = max(floor, (pipeline.cfg.get("audio") or {}).get("min_voice_over_music_db") or 0)
    parts.append(f"Sound: the music is {gain:+.0f} dB from the original mix"
                 + (f"; every line is kept at least {floor:.0f} dB over the music" if floor else "") + ".")
    parts.append(f"Money: ${spent:.2f} spent" + (f" of a ${float(cap):.2f} cap" if cap else ""))
    return "\n\n".join(parts)


def trailer_context(pipeline) -> str:
    """What the editor needs for a trailer direction: the shots and lines it may name, and the trailer as cut."""
    shots, lines = film_map(pipeline)
    return "\n\n".join([
        "Shots with video (id, film time, who is in frame, picture):\n" + "\n".join(
            f"{s['shot_id']} {_mmss(s['start_s'])} [{', '.join(s['in_frame']) or 'nobody'}]: {s['image']}"
            for s in shots if s["real"]),
        "Dialogue lines (id, time, speaker, English):\n" + "\n".join(
            f"{ln['line_id']} {_mmss(ln['start_s'])} {ln['speaker']}: {ln['text']}" for ln in lines),
        "Trailer as cut now (section: shot, line):\n" + "\n".join(_trailer_rows(pipeline, with_times=False)),
    ])


def validate(actions: list[TriageAction], known_shots: set[str], gate: str) -> tuple[list[TriageAction], str]:
    """The decision as it may be applied: known shot ids only, no trailer at the cut review, mix values clamped,
    the first MAX_ACTIONS that can be applied; a step at or before the shot lists makes reshooting the old shots
    pointless (their notes go along with it) and a trailer direction too (its ids name the old plan's shots).
    Returns the actions and, when nothing is left to do, why."""
    kept, why = [], ""
    for a in actions:
        if len(kept) >= MAX_ACTIONS:
            break
        if a.route == "trailer" and gate in CUT_GATES:
            why = why or "The trailer is cut after the film is approved, so it cannot be changed at this review."
            continue
        if a.route == "reshoot_shots":
            shots = [s for s in dict.fromkeys(a.shots) if s in known_shots]
            if not shots:
                why = why or "None of the shots it named are in this film."
                continue
            if len(shots) > MAX_SHOTS:
                why = why or f"That would remake {len(shots)} shots; send it back to Shot lists instead."
                continue
            a = a.model_copy(update={"shots": shots})
        else:
            a = a.model_copy(update={"shots": []})
        if a.route == "mix":
            gain = max(-12.0, min(3.0, a.mix.music_gain_db))
            floor = a.mix.min_voice_over_music_db
            floor = 0.0 if floor <= 0 else max(6.0, min(15.0, floor))
            if not gain and not floor:
                why = why or "It asked for a change to the sound that changes nothing."
                continue
            a = a.model_copy(update={"mix": MixChange(music_gain_db=gain, min_voice_over_music_db=floor)})
        if any(k.route == a.route for k in kept):
            continue                                   # the same route twice: the first one stands
        kept.append(a)
    early = [a for a in kept if STAGES.index(ROUTE_STAGE[a.route]) <= STAGES.index("shots")]
    if early:                                       # the plan is made again: reshooting its old shots is wasted
        folded = " ".join(a.notes_for_agents for a in kept if a.route == "reshoot_shots")
        first = min(early, key=lambda a: STAGES.index(ROUTE_STAGE[a.route]))
        kept = [a.model_copy(update={"notes_for_agents": f"{a.notes_for_agents} {folded}".strip()})
                if a is first and folded else a for a in kept if a.route not in ("reshoot_shots", "trailer")]
    return kept, ("" if kept else why)


def write_direction(pipeline, notes: str, rework_id: str | None):
    """The editor turns the owner's notes into a trailer direction (reports/trailer_direction.json), which the
    trailer step follows from then on. Returns the direction."""
    direction = pipeline.editor.trailer_direction(trailer_context(pipeline), notes)
    pipeline.p.reports.mkdir(parents=True, exist_ok=True)
    (pipeline.p.reports / TRAILER_DIRECTION).write_text(json.dumps(
        {"rework_id": rework_id, "notes": notes, "direction": direction.model_dump(), "unmet": []}, indent=1,
        ensure_ascii=False), encoding="utf-8")
    return direction


def apply(pipeline, req: dict, log=print) -> dict:
    """Decide with the editor and apply the decision. Returns the record for reports/owner_notes.jsonl (the caller
    adds the id and times). Every Claude call (the decision, a trailer direction, the shots planned again) comes
    before anything is undone, so when Claude cannot be reached this raises with the film as it was, and the request
    stays to be tried again; the mix is changed once per request, whatever happens after."""
    from .produce import clear
    gate = str(req.get("gate") or "final_cut")
    notes = str(req.get("notes") or "").strip()[:2000]
    gone = moved_on(pipeline, gate)
    if gone:
        log(f"  rejected at {gate}, not applied: {gone}")
        return {"mode": "triage", "gate": gate, "owner_notes": notes, "understood": "", "why": "", "question": "",
                "needs_code_change": False, "actions": [], "skipped": gone, "stage": None, "notes": "", "shots": [],
                "what": f"not applied: {gone}"}
    decision = pipeline.editor.triage(context(pipeline, gate), gate, notes)
    shots, _ = film_map(pipeline)
    actions, why_not = validate(decision.actions, {s["shot_id"] for s in shots}, gate)
    question = "" if actions else (decision.question.strip() or (f"{why_not} " if why_not else "") + NO_ACTION)
    trailer = next((a for a in actions if a.route == "trailer"), None)
    direction = write_direction(pipeline, trailer.notes_for_agents or notes, req.get("id")) if trailer else None
    done = []
    for a in sorted(actions, key=lambda a: a.route != "reshoot_shots"):   # the shots (Claude) before any undoing
        if a.route == "reshoot_shots":
            found = pipeline.rework_shots(a.shots, a.notes_for_agents)
            clear(pipeline, "cost", "approved")       # an AI-chosen spend always goes back through the cost check
            done.append(f"shots {', '.join(found)} are planned again and made anew (the cost check asks first)")
        elif a.route == "trailer":
            pipeline.invalidate("trailer")
            done.append(f"the trailer is cut again: {direction.why}")
        elif a.route == "mix":
            current = _read(pipeline.p.reports / OWNER_MIX, {}) or {}
            gain = float(current.get("music_gain_db") or 0.0)
            floor = float(current.get("min_voice_over_music_db") or 0.0)
            if current.get("rework_id") != req.get("id"):          # a request tried again never adds twice
                gain = round(max(-18.0, min(6.0, gain + a.mix.music_gain_db)), 1)
                floor = a.mix.min_voice_over_music_db or floor
                pipeline.p.reports.mkdir(parents=True, exist_ok=True)
                (pipeline.p.reports / OWNER_MIX).write_text(json.dumps(
                    {"rework_id": req.get("id"), "music_gain_db": gain, "min_voice_over_music_db": floor},
                    indent=1), encoding="utf-8")
            pipeline.invalidate("assemble")
            done.append(f"the sound is mixed again (music {gain:+.0f} dB from the original"
                        + (f", every line at least {floor:.0f} dB over the music" if floor else "") + ")")
        elif a.route == "music":
            pipeline.invalidate("music_plan")
            done.append("the score is planned again (the cost check asks first)")
        else:
            stage = ROUTE_STAGE[a.route]
            pipeline.fresh_takes(pipeline.made_shots())   # new plans: no old clip lands on a shot it was not made for
            pipeline.invalidate(stage)
            done.append(f"every step from {stage.replace('_', ' ')} on is redone (the cost check asks first)")
    first = min((ROUTE_STAGE[a.route] for a in actions), key=STAGES.index, default=None)
    agent_notes = " ".join(a.notes_for_agents for a in actions if a.notes_for_agents).strip()
    what = "; ".join(done) if done else "nothing redone yet: " + question
    log(f"  rejected at {gate}: {what}")
    return {"mode": "triage", "gate": gate, "owner_notes": notes, "understood": decision.understood,
            "why": decision.why, "question": question, "needs_code_change": decision.needs_code_change,
            "actions": [{"route": a.route, "shots": a.shots, "notes": a.notes_for_agents} for a in actions],
            "stage": first, "notes": agent_notes, "shots": [s for a in actions for s in a.shots], "what": what}
