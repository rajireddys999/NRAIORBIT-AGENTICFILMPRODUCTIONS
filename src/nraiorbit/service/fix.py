"""A stopped film, read by Claude: what happened and the one thing to do about it, when the owner presses "Fix this"
in the control room.

The control room's dialog writes stories/<slug>.fix.json ({id, by, at, stop, row_updated, words, evidence, shots,
cap, spent, estimate}) and starts a tick. The tick (queue.tick, before its story step) takes each request by
deleting it under its generation, so two ticks never answer the same one, asks the troubleshooter
(agents/troubleshooter.py) and writes <slug>/reports/fix.json for the film page to show.

Nothing here changes the film, its queue row, its gates or its money. The answer is an offer: the owner applies it
with the control room's own buttons (Retry, Send back, the cap), which check it again as they always do. A failure is
written into the record for the page to show, and the owner asks again.
"""

from __future__ import annotations

import datetime
import json
import math
import re
from typing import Callable

REQUESTS = "stories/"
SUFFIX = ".fix.json"
RECORD = "fix.json"                      # <slug>/reports/
PER_DAY = 8                              # asks a day for one film: each is one Claude call, a few cents
MAX_TOKENS = 16000                       # the answer is short; this bounds what one ask can cost
MAX_SHOTS = 12                           # shots one remedy may remake, as the editor's (service/triage.MAX_SHOTS)
WORDS = 1500                             # the owner's own words, at most
NOTES = 2000                             # as the Send back's notes
EVIDENCE = 30000                         # characters of evidence a request carries, at most
HISTORY = 40                             # asks remembered in the record, for the daily count
WAIT_S = 45 * 60                         # an ask no tick has taken after this long is lost: asking again is allowed
STEPS = ("story", "outline", "scenes", "cast", "shots", "assemble", "trailer")   # steps redone whole; the video
#                                          step is redone by its shots (redo_shots), never whole
SHOT_ID = re.compile(r"sc\d{3}_s\d{3}")
NONE = {"kind": "none", "step": "", "shots": [], "notes": "", "cap_usd": 0.0, "owner_steps": "", "code_note": ""}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _number(value) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) and n > 0 else 0.0


def cap_within_reach(cap: float, spent: float, estimate: float) -> float:
    """The highest cap an answer may put in front of the owner: twice the cap in force (or 25 dollars more, for a
    small one); with no cap yet, half again over what is spent and estimated. The owner may type any figure
    themselves: this only bounds the one Claude fills in."""
    return max(2 * cap, cap + 25) if cap else max(25.0, 1.5 * (spent + estimate))


def offer(decision, ask: dict) -> tuple[dict, str]:
    """The remedy as the control room may offer it, and (when it may not) why not: shot ids this film has, a step the
    Send back can redo whole, a cap above the one in force and within reach of it, and the words each kind needs.
    Anything else is offered as nothing: the owner reads what Claude said and decides."""
    r = decision.remedy
    notes = str(r.notes_for_agents or "").strip()[:NOTES]
    if r.kind == "retry":
        return {**NONE, "kind": "retry"}, ""
    if r.kind == "redo_shots":
        known = set(ask.get("shots") or [])
        shots = [s for s in dict.fromkeys(r.shots or []) if s in known]
        if not shots:
            return dict(NONE), "None of the shots it named are in this film."
        if len(shots) > MAX_SHOTS:
            return dict(NONE), f"It named {len(shots)} shots; at most {MAX_SHOTS} are remade at a time."
        if not notes:
            return dict(NONE), "It did not say what to change in those shots."
        return {**NONE, "kind": "redo_shots", "shots": shots, "notes": notes}, ""
    if r.kind == "redo_step":
        if r.step not in STEPS:
            return dict(NONE), "It did not name a step that can be redone."
        if not notes:
            return dict(NONE), "It did not say what to change in that step."
        return {**NONE, "kind": "redo_step", "step": r.step, "notes": notes}, ""
    if r.kind == "raise_cap":
        cap, new = _number(ask.get("cap")), _number(r.cap_usd)
        if not new:
            return dict(NONE), "It gave no figure for the cap."
        if new <= cap:
            return dict(NONE), f"The cap it gave (${new:.2f}) is not above the cap in force (${cap:.2f})."
        reach = cap_within_reach(cap, _number(ask.get("spent")), _number(ask.get("estimate")))
        return {**NONE, "kind": "raise_cap", "cap_usd": float(math.ceil(min(new, reach)))}, ""
    if r.kind == "outside":
        steps = str(r.owner_steps or "").strip()[:NOTES]
        return ({**NONE, "kind": "outside", "owner_steps": steps}, "") if steps else \
            (dict(NONE), "It did not say what to do.")
    if r.kind == "code_change":
        note = str(r.code_note or "").strip()[:2 * NOTES]
        return ({**NONE, "kind": "code_change", "code_note": note}, "") if note else \
            (dict(NONE), "It did not say what in the pipeline must change.")
    if r.kind == "ask" and str(decision.question or "").strip():
        return {**NONE, "kind": "ask"}, ""
    return dict(NONE), "It did not name a fix."


def answer(ask: dict, ask_claude: Callable) -> dict:
    """The record for one request. `ask_claude(evidence, words)` returns (StopDiagnosis, dollars it cost or None);
    when it fails the record says so and nothing else."""
    words = str(ask.get("words") or "").strip()[:WORDS]
    base = {"id": ask.get("id"), "asked": ask.get("at"), "by": ask.get("by"), "at": _now(), "stop": ask.get("stop"),
            "row_updated": ask.get("row_updated"), "words": words}
    try:
        decision, usd = ask_claude(str(ask.get("evidence") or "")[:EVIDENCE], words)
    except Exception as exc:                              # no key, no credit, the network, a refusal: said, not hidden
        return {**base, "error": f"Claude could not answer ({type(exc).__name__}: {str(exc)[:160]}). Ask again."}
    remedy, why_not = offer(decision, ask)
    return {**base, "what_happened": decision.what_happened.strip(), "why": decision.why.strip(),
            "after": decision.after.strip() if remedy["kind"] != "none" else "",
            "question": decision.question.strip() if remedy["kind"] == "ask" else "", "sure": decision.sure,
            "remedy": remedy, "proposed": decision.remedy.kind, "not_offered": why_not, "usd": usd, "error": ""}


def requested(bucket) -> list[str]:
    """The films the owner asked about. `bucket`: a storage client's bucket."""
    return sorted(b.name[len(REQUESTS):-len(SUFFIX)] for b in bucket.list_blobs(prefix=REQUESTS)
                  if b.name.endswith(SUFFIX) and "/" not in b.name[len(REQUESTS):])


def take(bucket, slug: str) -> dict | None:
    """The request, removed from the bucket by this tick alone (a delete under the generation it read); None when
    another tick took it first or it is not readable."""
    from google.api_core.exceptions import NotFound, PreconditionFailed
    blob = bucket.get_blob(f"{REQUESTS}{slug}{SUFFIX}")
    if blob is None:
        return None
    try:
        ask = json.loads(blob.download_as_text())
    except ValueError:
        ask = {}
    try:
        blob.delete(if_generation_match=blob.generation)
    except (NotFound, PreconditionFailed):
        return None
    return ask if isinstance(ask, dict) else {}


def still_stopped(row: dict | None, ask: dict) -> bool:
    """Whether the film is still at the stop the request was made at (the same state, the row untouched since)."""
    return bool(row) and str(row.get("state") or "").startswith("blocked:") \
        and row.get("state") == ask.get("stop") and row.get("updated") == ask.get("row_updated")


def run(bucket, ask_claude: Callable, rows: Callable[[], list[dict]], log: Callable[[str], None] = print) -> list[str]:
    """Every Fix request: taken, and answered when its film is still at that stop (one the film has moved on from is
    dropped unanswered: a re-run or a Send back got there first). `rows()` reads the queue sheet, only when there
    is a request. Returns the films answered."""
    asked = requested(bucket)
    if not asked:
        return []
    by_slug = {r["slug"]: r for r in rows()}
    done = []
    for slug in asked:
        try:
            ask = take(bucket, slug)
            if ask is None:
                continue
            if not still_stopped(by_slug.get(slug), ask):
                log(f"  Fix for {slug} not answered: the film has moved on from that stop")
                continue
            record = answer(ask, ask_claude)
            before = bucket.get_blob(f"{slug}/reports/{RECORD}")
            try:
                earlier = json.loads(before.download_as_text()).get("history") if before is not None else []
            except (ValueError, AttributeError):
                earlier = []
            record["history"] = [*[h for h in earlier or [] if isinstance(h, str)], str(ask.get("at") or _now())][-HISTORY:]
            bucket.blob(f"{slug}/reports/{RECORD}").upload_from_string(
                json.dumps(record, indent=1, ensure_ascii=False), content_type="application/json")
            log(f"  Fix for {slug}: " + (record["error"] or f"{record['remedy']['kind']} ({record['sure']})"))
            done.append(slug)
        except Exception as exc:                          # one film's unreadable record never holds up the rest
            log(f"  Fix for {slug} not looked at: {type(exc).__name__}: {str(exc)[:120]}")
    return done
