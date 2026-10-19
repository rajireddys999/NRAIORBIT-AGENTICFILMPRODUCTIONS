"""The CSV queues an always-on runner works through (`nraiorbit service tick`).

Two spreadsheets in the bucket are the control surface; the owner edits cells, the runner moves states.

    queue/stories.csv      slug, story_uri, budget_usd, gates, publish, state, note, updated
    queue/references.csv   url, title, learn, minutes, state, profile_uri, note, updated

A story row is the film's control surface: its budget_usd, gates and publish are written into the project's
pipeline.yaml on every tick, so a cap raised in the sheet (or the control room) reaches the run. A file next to the
story named <story>.overrides.yaml (film.runtime_minutes, voice.cast, ...) is merged into the project when it is made.
The library (identity packs, reference profiles) is restored from the bucket before a tick and backed up after it,
so a fresh container never pays for images it already has.

Story states: `new` (make the project from the template and start) → `awaiting:<gate>` (the owner reviews, then
sets `approved`) → `producing` … → `done`, or `blocked:<reason>` (the owner fixes the cause, then sets `retry`).
Reference states: `new` → `done` (profile written) or `blocked:<reason>`.

One tick does one unit of work per queue (a reference profile is minutes; a film can be hours), restores the
project from the bucket first and backs it up after, so a Cloud Run job with an empty disk can carry on where the
last one stopped. A tick claims its row (`producing`, with the time) before the restore, and another tick leaves a
claimed row alone for as long as a run can take, so two executions never do the same paid step. A tick never raises
a budget cap: the row's `budget_usd` is the cap, and only the owner edits it.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import shutil
from pathlib import Path

import yaml

STORY_COLUMNS = ["slug", "story_uri", "budget_usd", "gates", "publish", "state", "note", "updated"]
REFERENCE_COLUMNS = ["url", "title", "learn", "minutes", "state", "profile_uri", "note", "updated"]
GATES = ["story", "outline", "scenes", "cast", "shots", "cost", "faces", "render", "pacing", "final_cut", "trailer",
         "publish"]                                   # every gate a run can stop at, in order; what approve accepts
CLAIM_HOURS = 6.0                                     # a producing row is another tick's for this long (the job's limit)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------------ csv i/o

def _fetch(uri: str) -> str:
    if uri.startswith("gs://"):
        from ..release import gcs
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "q.csv"
            try:
                gcs.copy(uri, local)
            except gcs.BucketError:
                return ""
            return local.read_text(encoding="utf-8")
    p = Path(uri)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _store(uri: str, text: str) -> None:
    if uri.startswith("gs://"):
        from ..release import gcs
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "q.csv"
            local.write_text(text, encoding="utf-8")
            gcs.copy(local, uri)
        return
    Path(uri).parent.mkdir(parents=True, exist_ok=True)
    Path(uri).write_text(text, encoding="utf-8")


def load(uri: str, columns: list[str]) -> list[dict]:
    text = _fetch(uri)
    rows = list(csv.DictReader(io.StringIO(text))) if text.strip() else []
    return [{c: (r.get(c) or "").strip() for c in columns} for r in rows]


def save(uri: str, rows: list[dict], columns: list[str]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    _store(uri, buf.getvalue())


TICK_COLUMNS = ("state", "note", "updated")           # what a tick writes; the cap, gates and publish are the owner's


def write_story(uri: str, row: dict) -> None:
    """Write one film's state into the sheet as it is now, not as it was when this tick began: in the hours a run
    takes the owner may have lowered this film's cap or changed its visibility, and another tick or the owner may
    have changed other rows; all of that survives. Only the columns a tick owns are written."""
    rows = load(uri, STORY_COLUMNS)
    for i, r in enumerate(rows):
        if r["slug"] == row["slug"]:
            rows[i] = {**r, **{c: row.get(c, "") for c in TICK_COLUMNS}}
            break
    else:
        rows.append({c: row.get(c, "") for c in STORY_COLUMNS})
    save(uri, rows, STORY_COLUMNS)


# -------------------------------------------------------------------------------------------------- the work

def claimed(row: dict, hours: float | None = None) -> bool:
    """A `producing` row whose claim is younger than the longest a run can take: another execution is on it."""
    if row.get("state") != "producing" or not row.get("updated"):
        return False
    try:
        since = datetime.datetime.fromisoformat(row["updated"])
    except ValueError:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=datetime.timezone.utc)
    limit = float(os.environ.get("NRAIORBIT_CLAIM_HOURS") or (CLAIM_HOURS if hours is None else hours))
    return datetime.datetime.now(datetime.timezone.utc) - since < datetime.timedelta(hours=limit)


def ready_story(rows: list[dict]) -> dict | None:
    """The first row the runner may act on: new, retry, approved, or a run that was interrupted (producing, once
    its claim has expired)."""
    for state in ("approved", "retry", "producing", "new"):
        for r in rows:
            if r["state"] == state and not claimed(r):
                return r
    return None


def ready_reference(rows: list[dict]) -> dict | None:
    return next((r for r in rows if r["state"] in ("new", "retry")), None)


def make_project(projects_dir: Path, template: Path, row: dict, story_text: str,
                 overrides: dict | None = None) -> Path:
    """A project folder from the template, the story in place, `overrides` (the story's .overrides.yaml) merged
    into pipeline.yaml, then the row's cap and policy on top."""
    from ..config import deep_merge

    root = projects_dir / row["slug"]
    if not (root / "pipeline.yaml").exists():
        root.mkdir(parents=True, exist_ok=True)
        if template.exists():
            shutil.copytree(template, root, dirs_exist_ok=True)
    (root / "input").mkdir(parents=True, exist_ok=True)
    if not (root / "input" / "story.md").exists():
        (root / "input" / "story.md").write_text(story_text, encoding="utf-8")
    cfg_path = root / "pipeline.yaml"
    cfg = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}) or {}
    if overrides:
        cfg = deep_merge(cfg, overrides)
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    apply_row(root, row)
    return root


def apply_row(root: Path, row: dict) -> bool:
    """The row's budget_usd, gates and publish into the project's pipeline.yaml; True when something changed."""
    cfg_path = root / "pipeline.yaml"
    cfg = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}) or {}
    before = yaml.safe_dump(cfg, sort_keys=True)
    if row.get("budget_usd"):
        cfg.setdefault("budget", {})["max_usd"] = float(row["budget_usd"])
    pol = cfg.setdefault("policy", {})
    if row.get("gates"):
        pol["gates"] = row["gates"]
    if row.get("publish"):
        pol["publish"] = row["publish"]
    changed = yaml.safe_dump(cfg, sort_keys=True) != before
    if changed or not cfg_path.exists():
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return changed


def overrides_uri(story_uri: str) -> str:
    """<story>.overrides.yaml next to the story: rain.md -> rain.overrides.yaml."""
    head, _, tail = story_uri.rpartition("/")
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    return f"{head}/{stem}.overrides.yaml" if head else f"{stem}.overrides.yaml"


def run_state(root: Path) -> str:
    """The last run's state from reports/produce.json ('' when there is none)."""
    try:
        return str(json.loads((root / "reports" / "produce.json").read_text(encoding="utf-8")).get("state") or "")
    except (OSError, ValueError):
        return ""


def pending_gate(root: Path) -> str | None:
    """The gate an approval is for: the one the last run stopped at, else the first marker that was never approved.
    A marker with an .approved sibling is spent (the bucket mirror can bring one back); it is never approved twice."""
    reviews = root / "reviews"
    pend = {p.stem for p in reviews.glob("*.pending")} if reviews.exists() else set()
    state = run_state(root)
    if state.startswith("awaiting:") and state[9:] in pend:
        return state[9:]
    live = sorted(g for g in pend if not (reviews / f"{g}.approved").exists())
    return live[0] if live else None


def approve_gate(root: Path, gate: str) -> None:
    """Turn the gate's pending marker into its approval, keeping what the marker said (the cost gate's estimate)."""
    reviews = root / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    pend = reviews / f"{gate}.pending"
    text = pend.read_text(encoding="utf-8") if pend.exists() else ""
    pend.unlink(missing_ok=True)
    (reviews / f"{gate}.approved").write_text("approved in the queue\n" + text, encoding="utf-8")


def tick(stories_uri: str, references_uri: str, projects_dir: Path, *, bucket: str | None, produce_fn, learn_fn,
         template: Path | None = None, fetch_story=_fetch, log=print) -> dict:
    """One pass: one reference profile, then one story step. Returns what happened."""
    summary = {"reference": None, "story": None}
    if bucket:                                            # identity packs and reference profiles made by earlier ticks
        from ..ops import backup
        try:
            backup.sync_library(bucket, "restore", log=lambda *_: None)
        except Exception as exc:
            log(f"  library restore skipped: {type(exc).__name__}: {str(exc)[:120]}")
    # references first: cheap and useful to the next film
    refs = load(references_uri, REFERENCE_COLUMNS)
    r = ready_reference(refs)
    if r:
        try:
            profile = learn_fn(r["url"], r.get("title") or None, r.get("learn") or "all", r.get("minutes") or None)
            r.update(state="done", profile_uri=str(profile), note="", updated=now())
        except Exception as exc:
            r.update(state=f"blocked:{type(exc).__name__}", note=str(exc)[:200], updated=now())
        save(references_uri, refs, REFERENCE_COLUMNS)
        summary["reference"] = {"url": r["url"], "state": r["state"]}
        log(f"  reference {r['url']}: {r['state']}")
    # one story step
    rows = load(stories_uri, STORY_COLUMNS)
    s = ready_story(rows)
    if s:
        root = projects_dir / s["slug"]
        was_approved = s["state"] == "approved"
        s.update(state="producing", updated=now())        # the claim, before the slow restore: no second tick joins in
        write_story(stories_uri, s)
        try:
            if bucket and not (root / "pipeline.yaml").exists():
                from ..ops import backup
                backup.restore(root, bucket, log=lambda *_: None)
            if not (root / "pipeline.yaml").exists():
                story = fetch_story(s["story_uri"]) if s.get("story_uri") else ""
                if not story.strip():
                    raise ValueError(f"story_uri {s.get('story_uri')!r} is empty or unreadable")
                extra = fetch_story(overrides_uri(s["story_uri"])) if s.get("story_uri") else ""
                make_project(projects_dir, template or (projects_dir / "_template"), s, story,
                             overrides=(yaml.safe_load(extra) or {}) if extra.strip() else None)
            else:
                apply_row(root, s)                        # the sheet's cap and policy win, every tick
            if was_approved:
                gate = pending_gate(root)
                if gate:
                    approve_gate(root, gate)
            report = produce_fn(root, bucket)
            s.update(state=report["state"], note=(report.get("steps", {}).get("publish") or "")
                     if isinstance(report.get("steps", {}).get("publish"), str) else
                     str(report.get("steps", {}).get("publish", "")), updated=now())
            if bucket:
                from ..ops import backup
                backup.backup(root, bucket, log=lambda *_: None)
        except Exception as exc:
            s.update(state=f"blocked:{type(exc).__name__}", note=str(exc)[:200], updated=now())
        write_story(stories_uri, s)
        summary["story"] = {"slug": s["slug"], "state": s["state"]}
        log(f"  story {s['slug']}: {s['state']}")
    if bucket and (summary["reference"] or summary["story"]):
        from ..ops import backup
        try:
            backup.sync_library(bucket, "backup", log=lambda *_: None)
        except Exception as exc:
            log(f"  library backup failed: {type(exc).__name__}: {str(exc)[:120]}")
    return summary
