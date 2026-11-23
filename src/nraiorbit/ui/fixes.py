"""The control room's side of "Fix this" (the runner's is service/fix.py): the evidence a stopped film's request
carries to Claude, and what the film page shows of a request and its answer.

The page offers an answer only for the very stop it was asked about (the same state, the queue row untouched
since), and its Apply buttons are the page's own (Retry, Send back, the cap): they check what is sent as they
always do, so an answer can fill a form in but never get past one."""

from __future__ import annotations

import datetime
import json
import os

from ..service import fix, queue
from . import films
from .store import Store

LOG_LINES = 40                 # the runner's last lines sent along
NAMED_SHOTS = 12               # shots described in full: the ones the stop and the checks name
HEADLINES = {
    "retry": "run it again as it is", "redo_shots": "remake the shots it names",
    "redo_step": "redo one step, and the steps after it", "raise_cap": "raise the budget cap",
    "outside": "something to do outside the control room", "code_change": "the pipeline itself needs a change",
    "ask": "Claude has a question for you", "none": "no fix the control room can apply",
}


def _cut(text, n: int) -> str:
    text = " ".join(str(text if text is not None else "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _lines(title: str, rows: list[str]) -> str:
    return f"{title}\n" + "\n".join(rows) if rows else ""


def earlier_answer(store: Store, slug: str, row: dict) -> dict | None:
    """The answer already given for this very stop, if any (the owner is asking again, or answering a question)."""
    record = films._json(store, f"{slug}/reports/{fix.RECORD}")
    if not isinstance(record, dict) or record.get("error") or not record.get("id"):
        return None
    same = fix.still_stopped(row, {"stop": record.get("stop"), "row_updated": record.get("row_updated")})
    return record if same else None


def evidence(store: Store, slug: str, row: dict, d: dict, log_lines: list[str]) -> str:
    """What Claude reads about a stopped film, as plain text: the stop, the last run's steps, the money, who is in
    the film, the shots the stop names, what the checks and the earlier fixes said, and the runner's last lines.
    `d` is the film page's own data (films.detail), so the owner and Claude look at the same film."""
    issue, cash, bible = d.get("issue") or {}, d.get("money") or {}, d.get("bible") or {}
    steps = d.get("steps") or {}
    shots = [sh for sl in d.get("shots") or [] for sh in sl.get("shots") or [] if sh.get("shot_id")]
    checks = [r for r in d.get("review_queue") or [] if isinstance(r, dict)][-10:]
    rewrites = [r for r in films._jsonl(store, f"{slug}/reports/safety_rewrites.jsonl") if isinstance(r, dict)][-4:]
    said = " ".join([str(row.get("state") or ""), str(row.get("note") or ""), json.dumps(steps, default=str)])
    by_id = {sh["shot_id"]: sh for sh in shots}
    flagged = [str(r.get("shot_id")) for r in (*checks, *rewrites)]
    # described in full: the shots the stop itself names, then the flagged ones that still have no video (one
    # flagged earlier that has its video since is old news: named below, marked as made)
    named = list(dict.fromkeys([*fix.SHOT_ID.findall(said),
                                *(s for s in flagged if s in by_id and not by_id[s].get("filmed"))]))

    def since(sid) -> str:
        return " (this shot has its video now)" if (by_id.get(str(sid)) or {}).get("filmed") else ""
    scene_of = {sh["shot_id"]: sl.get("scene_id") for sl in d.get("shots") or [] for sh in sl.get("shots") or []
                if sh.get("shot_id")}
    estimate = (cash.get("estimate") or {}).get("total_usd")
    length = (f"a Short of {d.get('seconds')} seconds" if d.get("format") == "short" else
              f"a film of about {d.get('runtime')} minutes" if d.get("runtime") else "a film")
    parts = [
        f"Film: {d.get('title') or slug} ({slug}), {length}. Tool set: {(d.get('quality') or {}).get('key') or 'the default'}."
        + (f"\nStory in one line: {_cut(bible.get('logline'), 400)}" if bible.get("logline") else ""),
        f"The stop (recorded {row.get('updated') or 'at a time not known'})\n"
        f"State: {_cut(row.get('state'), 400)}\n"
        f"The runner's note: {_cut(row.get('note'), 600) or '(none)'}\n"
        f"Step it stopped at: {issue.get('label') or 'not known'}"
        + (f"\nWhat the page already tells the owner: {_cut(d.get('hint'), 600)}" if d.get("hint") else ""),
        _lines("The last run that wrote its steps down, finished "
               f"{(d.get('produce') or {}).get('finished') or 'at a time not known'} (step: outcome; a run that "
               "failed outright after that wrote none)",
               [f"{k}: {_cut(v if isinstance(v, str) else json.dumps(v, default=str), 300)}" for k, v in steps.items()]),
    ]
    progress = films.progress_record(store, slug) or {}
    if progress:
        parts.append(f"The runner's own record: it was at '{progress.get('phase')}', ended '{progress.get('state')}', "
                     f"last line: {_cut(progress.get('last_line'), 300)}")
    parts.append("Money: " + (f"cap ${float(cash['cap']):.2f}" if cash.get("cap") else "no cap set")
                 + f", ${float(cash.get('spent') or 0):.2f} spent"
                 + (f", the paid steps were estimated at ${float(estimate):.2f}" if estimate else ""))
    people = []
    for c in d.get("cast") or []:
        age = (c.get("physical") or {}).get("age")
        people.append(f"{c.get('id')}: {c.get('name')}, {c.get('role') or 'role not set'}"
                      + (f", age {age}" if age not in (None, "") else "")
                      + (" (under 18: kept out of every shot)" if c.get("off_screen") else ""))
    parts.append(_lines("Characters (id: name, role, age)", people))
    if shots:
        missing = [sh["shot_id"] for sh in shots if not sh.get("filmed")]
        parts.append(f"Shots: {len(shots)} planned, {len(shots) - len(missing)} have their video."
                     + (f" Without video: {', '.join(missing[:40])}" + (" and more" if len(missing) > 40 else "")
                        if missing else ""))
    parts.append(_lines("The shots the stop names, and flagged shots still without video (id, scene, length, size, "
                        "who is in frame)", [
        f"{sid} in {scene_of.get(sid)}, {by_id[sid].get('duration_s')} s, {by_id[sid].get('size')}, "
        f"[{', '.join(by_id[sid].get('characters_in_frame') or []) or 'nobody'}], "
        f"{'has its video' if by_id[sid].get('filmed') else 'no video'}\n"
        f"  picture asked for: {_cut(by_id[sid].get('image_prompt'), 500)}\n"
        f"  movement asked for: {_cut(by_id[sid].get('motion_prompt'), 250)}"
        for sid in named[:NAMED_SHOTS] if sid in by_id]))
    parts.append(_lines("What the checks flagged, latest last (shot: reason)", [
        f"{r.get('shot_id')}{since(r.get('shot_id'))}: {_cut('; '.join(map(str, r.get('reason') or [])), 300)}"
        for r in checks]))
    parts.append(_lines("Refusals the pipeline already reworded by itself (shot: what the model said)", [
        f"{r.get('shot_id')}{since(r.get('shot_id'))}: {_cut(r.get('refusal'), 300)}" for r in rewrites]))
    redrawn = films._json(store, f"{slug}/reports/adult_frames.json")
    if isinstance(redrawn, dict) and redrawn:
        parts.append("First frames already drawn again with every person stated as a grown adult (shot: times): "
                     + ", ".join(f"{k}: {v}" for k, v in list(redrawn.items())[:20]))
    children = films._json(store, f"{slug}/reports/child_stops.json")
    if isinstance(children, dict) and children:
        parts.append("Shots planned again because they showed a child, and what was left: "
                     + _cut(json.dumps(children, ensure_ascii=False), 600))
    parts.append(_lines("Planning problems the pipeline noted, latest last", [
        _cut(json.dumps(r, ensure_ascii=False), 300)
        for r in films._jsonl(store, f"{slug}/reports/shot_issues.jsonl")[-6:] if isinstance(r, dict)]))
    parts.append(_lines("Earlier send-backs for this film and what was done, latest last", [
        f"{r.get('at')}: {r.get('stage') or r.get('mode') or 'sent back'}"
        + (f", shots {', '.join(map(str, r.get('shots') or []))}" if r.get("shots") else "")
        + f"; the owner wrote: {_cut(r.get('notes') or r.get('owner_notes'), 300)}; done: {_cut(r.get('what'), 200)}"
        for r in films._jsonl(store, f"{slug}/reports/owner_notes.jsonl")[-5:] if isinstance(r, dict)]))
    parts.append(_lines("The runner's last lines", [_cut(line, 300) for line in (log_lines or [])[-LOG_LINES:]]))
    before = earlier_answer(store, slug, row)
    if before:                                            # asked again: what was said last time, to build on or correct
        remedy = before.get("remedy") if isinstance(before.get("remedy"), dict) else {}
        parts.append("Your earlier answer about this same stop (the owner is asking again; their words follow)\n"
                     f"What happened: {_cut(before.get('what_happened'), 400)}\n"
                     f"Why: {_cut(before.get('why'), 400)}\n"
                     f"You proposed: {before.get('proposed') or remedy.get('kind')}"
                     + (f"; shots {', '.join(map(str, remedy.get('shots') or []))}" if remedy.get("shots") else "")
                     + (f"; notes: {_cut(remedy.get('notes'), 400)}" if remedy.get("notes") else "")
                     + (f"\nYou asked the owner: {_cut(before.get('question'), 300)}" if before.get("question") else "")
                     + (f"\nThe control room could not offer it: {before.get('not_offered')}"
                        if before.get("not_offered") else ""))
    return "\n\n".join(p for p in parts if p)[:fix.EVIDENCE]


def request(store: Store, slug: str, row: dict, d: dict, log_lines: list[str], words: str, email: str) -> dict:
    """The request the next tick takes (stories/<slug>.fix.json): the evidence, and what the answer is checked
    against (this film's shot ids, its cap and its money)."""
    cash = d.get("money") or {}
    at = queue.now()
    return {"id": f"{at}-{os.urandom(3).hex()}", "by": email, "at": at, "stop": row["state"],
            "row_updated": row.get("updated"), "words": words, "evidence": evidence(store, slug, row, d, log_lines),
            "shots": [sh["shot_id"] for sl in d.get("shots") or [] for sh in sl.get("shots") or []
                      if sh.get("shot_id")],
            "cap": cash.get("cap"), "spent": cash.get("spent"),
            "estimate": (cash.get("estimate") or {}).get("total_usd")}


def _asked(store: Store, slug: str, row: dict | None) -> tuple[dict | None, bool]:
    """(the request for this very stop that no tick has taken yet, whether it has waited too long to count)."""
    ask = films._json(store, f"{fix.REQUESTS}{slug}{fix.SUFFIX}")
    if not isinstance(ask, dict) or not fix.still_stopped(row, ask):
        return None, False
    age = films.seconds_since(ask.get("at"))
    return ask, age is None or age > fix.WAIT_S


def waiting(store: Store, slug: str, row: dict | None) -> dict | None:
    """A request for this very stop that a tick may still take. One older than fix.WAIT_S was lost (the runner
    was down, or stopped between taking it and answering): it no longer holds the dialog, and asking again
    replaces it."""
    ask, lost = _asked(store, slug, row)
    return None if lost else ask


def asked_today(store: Store, slug: str, row: dict | None = None) -> int:
    """Asks made for this film today (UTC), answered or still waiting."""
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    record = films._json(store, f"{slug}/reports/{fix.RECORD}")
    history = record.get("history") if isinstance(record, dict) else None
    n = sum(1 for at in history or [] if str(at).startswith(today))
    ask = waiting(store, slug, row)
    return n + (1 if ask and str(ask.get("at") or "").startswith(today) else 0)


def view(store: Store, slug: str, row: dict | None) -> dict | None:
    """What the film page shows on a stopped film's card: None unless the film is in the queue and blocked; else
    whether Claude may still be asked today, a request still waiting, and the answer to this very stop (never an
    earlier stop's: a re-run, a Send back or any edit of the row hides it)."""
    if not row or not str(row.get("state") or "").startswith("blocked:"):
        return None
    ask = waiting(store, slug, row)
    record = films._json(store, f"{slug}/reports/{fix.RECORD}")
    record = record if isinstance(record, dict) else {}
    answer = None
    if ask is None and record.get("id") and fix.still_stopped(row, {"stop": record.get("stop"),
                                                                    "row_updated": record.get("row_updated")}):
        # the same stop, asked about before: shown again, with Ask again beside it
        remedy = record.get("remedy") if isinstance(record.get("remedy"), dict) else {}
        kind = remedy.get("kind") if remedy.get("kind") in HEADLINES else "none"
        step = remedy.get("step") if remedy.get("step") in fix.STEPS else ""
        at_estimate = str(row.get("state") or "").startswith(("blocked:estimate", "blocked:no cap"))
        answer = {**record, "kind": kind, "headline": "Claude could not answer" if record.get("error") else
                  HEADLINES[kind],
                  "step": step, "step_label": dict(films.REWORK_STEPS).get(step, ""),
                  "shots_text": ", ".join(map(str, remedy.get("shots") or [])),
                  "notes": str(remedy.get("notes") or ""), "cap": remedy.get("cap_usd") or 0,
                  "owner_steps": str(remedy.get("owner_steps") or ""), "code_note": str(remedy.get("code_note") or ""),
                  # the estimate's stops re-run in one click; any other stop saves the cap, then Retry
                  "cap_action": "cap-and-rerun" if at_estimate else "row"}
    left = max(0, fix.PER_DAY - asked_today(store, slug, row))
    stale, lost = _asked(store, slug, row)
    return {"pending": {"at": ask.get("at"), "words": ask.get("words") or ""} if ask else None, "answer": answer,
            "unanswered": stale.get("at") if stale and lost else None,
            "left": left, "per_day": fix.PER_DAY, "words_max": fix.WORDS}
