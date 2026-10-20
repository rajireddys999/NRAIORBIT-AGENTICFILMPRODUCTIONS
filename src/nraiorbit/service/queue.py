"""The CSV queues an always-on runner works through (`nraiorbit service tick`).

Two spreadsheets in the bucket are the control surface; the owner edits cells, the runner moves states.

    queue/stories.csv      slug, story_uri, budget_usd, gates, publish, state, note, updated
    queue/references.csv   url, title, learn, minutes, state, profile_uri, note, updated

A story row is the film's control surface: its budget_usd, gates and publish are written into the project's
pipeline.yaml on every tick, so a cap raised in the sheet (or the control room) reaches the run. A file next to the
story named <story>.overrides.yaml (film.runtime_minutes, voice.cast, ...) is merged into the project when it is made.
The library (identity packs, reference profiles) is restored from the bucket before a tick that has work and backed
up after it, so a fresh container never pays for images it already has; a tick with nothing to do reads the two
sheets and exits.

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
import inspect
import io
import json
import os
import shutil
import time
from pathlib import Path

import yaml

from .progress import Tracker

STORY_COLUMNS = ["slug", "story_uri", "budget_usd", "gates", "publish", "state", "note", "updated"]
REFERENCE_COLUMNS = ["url", "title", "learn", "minutes", "state", "profile_uri", "note", "updated"]
GATES = ["story", "outline", "scenes", "cast", "shots", "cost", "faces", "render", "pacing", "final_cut", "trailer",
         "publish"]                                   # every gate a run can stop at, in order; what approve accepts
CLAIM_HOURS = 6.0                                     # a producing row is another tick's for this long (the job's limit)
TRACKER_FACTORY = Tracker                             # the execution's running record; tests inject a fake


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------------ csv i/o

def _fetch(uri: str) -> str:
    """The text at a local path or gs:// URI, '' when there is none or it cannot be read. Through the storage client
    a read is a tenth of a second; gcloud cp, used where no client can be made or the client's read fails, takes
    seconds. Only the client's NotFound means "no sheet": any other error (stale Application Default Credentials
    beside a working gcloud login) read as empty would let add-story save a one-row sheet over everyone's rows."""
    if uri.startswith("gs://"):
        from ..release import gcs
        import tempfile
        try:
            storage = gcs.client()
        except Exception:
            storage = None
        if storage is not None:
            try:
                text = gcs.read_text(uri, storage)
                return "" if text is None else text
            except Exception:
                pass                                       # gcloud below, with its own login
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


def rows_of(text: str, columns: list[str]) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text))) if text.strip() else []
    return [{c: (r.get(c) or "").strip() for c in columns} for r in rows]


def csv_of(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    return buf.getvalue()


def load(uri: str, columns: list[str]) -> list[dict]:
    return rows_of(_fetch(uri), columns)


def save(uri: str, rows: list[dict], columns: list[str]) -> None:
    _store(uri, csv_of(rows, columns))


class SheetContended(RuntimeError):
    """The sheet changed under every attempt to write it."""


def update_text(uri: str, change, content_type: str = "text/csv", attempts: int = 6) -> str:
    """Read, change, write, with a precondition on the bucket: the write lands only if nobody wrote the object since
    it was read (its generation), else it is read again and the change redone, so two writers (a tick's claim and a
    save in the control room, seconds apart) never lose each other's edit. `change(text)` returns the new text, or
    None to leave the object alone. A local file is read and written in place. Returns the text as it stands."""
    if not uri.startswith("gs://"):
        p = Path(uri)
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        new = change(text)
        if new is not None and new != text:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new, encoding="utf-8")
            return new
        return text
    from google.api_core.exceptions import NotFound, PreconditionFailed

    from ..release import gcs
    bucket_name, _, key = uri[5:].partition("/")
    for _ in range(attempts):
        bucket = gcs.client().bucket(bucket_name)
        blob = bucket.get_blob(key)
        if blob is None:                                   # absent: create it, and only if still absent
            text, generation, blob = "", 0, bucket.blob(key)
        else:
            try:
                text, generation = blob.download_as_text(), blob.generation
            except NotFound:                               # rewritten between the look-up and the read: again
                continue
        new = change(text)
        if new is None or new == text:
            return text
        try:
            blob.upload_from_string(new, content_type=content_type, if_generation_match=generation)
            return new
        except PreconditionFailed:
            continue
    raise SheetContended(f"{uri} kept changing; nothing written after {attempts} attempts")


TICK_COLUMNS = ("state", "note", "updated")           # what a tick writes; the cap, gates and publish are the owner's
REFERENCE_TICK_COLUMNS = ("state", "profile_uri", "note", "updated")


def _write_row(uri: str, row: dict, columns: list[str], key: str, owned: tuple) -> None:
    def change(text: str) -> str:
        rows = rows_of(text, columns)
        for i, r in enumerate(rows):
            if r[key] == row[key]:
                rows[i] = {**r, **{c: row.get(c, "") for c in owned}}
                break
        else:
            rows.append({c: row.get(c, "") for c in columns})
        return csv_of(rows, columns)
    update_text(uri, change)


def write_story(uri: str, row: dict) -> None:
    """Write one film's state into the sheet as it is now, not as it was when this tick began: in the hours a run
    takes the owner may have lowered this film's cap or changed its visibility, and another tick or the owner may
    have changed other rows; all of that survives. Only the columns a tick owns are written, under a precondition."""
    _write_row(uri, row, STORY_COLUMNS, "slug", TICK_COLUMNS)


REFERENCE_OWNER_COLUMNS = ("url", "title", "learn", "minutes")


def write_reference(uri: str, row: dict) -> None:
    """The same for one reference row. The same link may be queued twice with different focuses, so the result goes
    to the row the tick picked: the first row with the owner's columns and a state the tick acts on (what
    ready_reference chose), else the first pending row with that url, else that very reference wherever it is; a
    row that vanished gets its result appended rather than lost. Never the wrong row: the picked one would stay
    `new` and be watched, and paid for, every tick."""
    def change(text: str) -> str:
        rows = rows_of(text, REFERENCE_COLUMNS)

        def same(r: dict) -> bool:
            return all(r[c] == row.get(c, "") for c in REFERENCE_OWNER_COLUMNS)

        def pending(r: dict) -> bool:
            return r["state"] in ("new", "retry")
        target = (next((r for r in rows if same(r) and pending(r)), None)
                  or next((r for r in rows if r["url"] == row["url"] and pending(r)), None)
                  or next((r for r in rows if same(r)), None))
        if target is None:
            rows.append({c: row.get(c, "") for c in REFERENCE_COLUMNS})
        else:
            target.update({c: row.get(c, "") for c in REFERENCE_TICK_COLUMNS})
        return csv_of(rows, REFERENCE_COLUMNS)
    update_text(uri, change)


def restore_strikes(note: str) -> int:
    """How many ticks in a row the restore has failed, as the row's note records it."""
    import re
    m = re.match(r"restore failed \((\d+)\)", note or "")
    return int(m.group(1)) if m else 0


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
    fresh = not (root / "pipeline.yaml").exists()
    if fresh:
        if not template.exists():                           # never a film from scratch: it would run on the default
            raise FileNotFoundError(                        # (mock) providers, a free animatic nobody asked for
                f"no project template at {template}; the runner's image ships one under projects/_template")
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template, root, dirs_exist_ok=True)
    (root / "input").mkdir(parents=True, exist_ok=True)
    if fresh or not (root / "input" / "story.md").exists():      # the queued story, never the template's placeholder
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
        import math
        cap = float(row["budget_usd"])
        if math.isfinite(cap) and cap > 0:                     # a sheet cannot switch the hard stop off
            cfg.setdefault("budget", {})["max_usd"] = cap
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


class RestoreFailed(RuntimeError):
    """The bucket could not be read for the sets a run depends on; nothing runs on a half-restored project."""


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


def _doing(tracker, doing: str, **fields) -> None:
    if tracker is not None:
        tracker.start(doing, **fields)


def _timing_line(what: str, timings: dict, seconds: float) -> str:
    """One line per restore or backup, so the time each set takes can be read from the runner's log."""
    if not timings:
        return f"  {what}: 0 sets in {seconds:.1f} s"
    slow = max(timings, key=timings.get)
    return f"  {what}: {len(timings)} sets in {seconds:.1f} s (slowest {slow} {timings[slow]:.1f} s)"


def _backup_project(root: Path, bucket: str, mirror_reviews: bool, log) -> None:
    """The project to the bucket, with its timing line. `mirror_reviews` only when this tick restored the reviews
    folder (backup.backup says why)."""
    from ..ops import backup
    timings, t0 = {}, time.monotonic()
    backup.backup(root, bucket, log=lambda *_: None, mirror_reviews=mirror_reviews, timings=timings)
    log(_timing_line("backup", timings, time.monotonic() - t0))


def _takes_progress(fn) -> bool:
    """Whether a produce_fn can hear the film's progress; a two-argument one (the tests' fakes) cannot."""
    try:
        return "progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def tick(stories_uri: str, references_uri: str, projects_dir: Path, *, bucket: str | None, produce_fn, learn_fn,
         template: Path | None = None, fetch_story=_fetch, log=print) -> dict:
    """One pass: one reference profile, then one story step. Returns what happened. With a bucket, the execution
    says what it is doing in queue/running/ while it works (service/progress.py), and the record goes when it exits,
    whatever happened."""
    tracker = TRACKER_FACTORY(bucket, log=log) if bucket else None
    try:
        return _tick(stories_uri, references_uri, projects_dir, bucket=bucket, produce_fn=produce_fn,
                     learn_fn=learn_fn, template=template, fetch_story=fetch_story, log=log, tracker=tracker)
    finally:
        if tracker is not None:
            tracker.close()


def _tick(stories_uri, references_uri, projects_dir, *, bucket, produce_fn, learn_fn, template, fetch_story, log,
          tracker) -> dict:
    summary = {"reference": None, "story": None}
    # the sheets first: a tick with nothing to do (most of them) never touches the library
    refs = load(references_uri, REFERENCE_COLUMNS)
    r, peek = ready_reference(refs), ready_story(load(stories_uri, STORY_COLUMNS))
    if not r and not peek:
        _doing(tracker, "idle")                           # nothing to do: said so until the tick exits
        return summary
    library_restored = False
    if bucket:                                            # identity packs and reference profiles made by earlier ticks
        from ..ops import backup
        _doing(tracker, "library-restore")
        try:
            backup.sync_library(bucket, "restore", log=lambda *_: None)
            library_restored = True
        except Exception as exc:
            log(f"  library restore skipped: {type(exc).__name__}: {str(exc)[:120]}")
    # references first: cheap and useful to the next film
    if r:
        _doing(tracker, "reference", url=r["url"])
        try:
            profile = learn_fn(r["url"], r.get("title") or None, r.get("learn") or "all", r.get("minutes") or None)
            r.update(state="done", profile_uri=str(profile), note="", updated=now())
        except Exception as exc:
            r.update(state=f"blocked:{type(exc).__name__}", note=str(exc)[:200], updated=now())
        write_reference(references_uri, r)
        summary["reference"] = {"url": r["url"], "state": r["state"]}
        log(f"  reference {r['url']}: {r['state']}")
    # one story step, from the sheet as it is now: a reference profile takes minutes, and the owner may edit meanwhile
    rows = load(stories_uri, STORY_COLUMNS)
    s = ready_story(rows)
    if s:
        root = projects_dir / s["slug"]
        prior, was_approved = s["state"], s["state"] == "approved"
        s.update(state="producing", updated=now())        # the claim, before the slow restore: no second tick joins in
        write_story(stories_uri, s)
        _doing(tracker, "story", slug=s["slug"])
        mirror_reviews, film, restored = False, None, False
        if tracker is not None and _takes_progress(produce_fn):
            film = tracker.film(s["slug"], root)          # from the claim on: a failure before produce (the restore,
        try:                                              # the template, the cap) overwrites the last run's record
            if bucket and not (root / "pipeline.yaml").exists():
                from ..ops import backup
                if film is not None:
                    film.phase("restore")
                timings, t0 = {}, time.monotonic()
                _, failed = backup.restore_report(root, bucket, log=lambda *_: None, timings=timings)
                log(_timing_line("restore", timings, time.monotonic() - t0))
                if failed:                                # a half-restored project would pay again or skip a gate
                    raise RestoreFailed(f"the bucket could not be read for {', '.join(sorted(failed))}")
                mirror_reviews = True                     # this run saw every marker the bucket holds: it may mirror them
            restored = True                               # or on this disk already: a backup sends nothing half-restored
            if film is not None:
                film.phase("setup")
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
            if film is not None:
                report = produce_fn(root, bucket, progress=film)
            else:
                report = produce_fn(root, bucket)
            s.update(state=report["state"], note=(report.get("steps", {}).get("publish") or "")
                     if isinstance(report.get("steps", {}).get("publish"), str) else
                     str(report.get("steps", {}).get("publish", "")), updated=now())
            if bucket:
                _backup_project(root, bucket, mirror_reviews, log)
        except RestoreFailed as exc:                      # not the film's fault: the row goes back as it was, with a
            if film is not None:
                film.finish("failed")
            strikes = restore_strikes(s.get("note", "")) + 1       # strike; three in a row and the owner is told
            back_to = "retry" if prior == "producing" else prior   # never a fresh claim nobody is working under
            s.update(state="blocked:restore" if strikes >= 3 else back_to,
                     note=f"restore failed ({strikes}): {exc}"[:200], updated=now())
        except Exception as exc:
            if film is not None:
                film.finish("failed")
            s.update(state=f"blocked:{type(exc).__name__}", note=str(exc)[:200], updated=now())
            if bucket and restored:                       # what the run paid for and saved before it failed (a later
                try:                                      # scene's shot list, clips) outlives the container
                    _backup_project(root, bucket, mirror_reviews, log)
                except Exception as e:                    # the row keeps the run's own error
                    log(f"  backup after the failure failed: {type(e).__name__}: {str(e)[:120]}")
        write_story(stories_uri, s)
        summary["story"] = {"slug": s["slug"], "state": s["state"]}
        log(f"  story {s['slug']}: {s['state']}")
    elif not r:
        _doing(tracker, "idle")                           # nothing to do: said so until the tick exits
    if bucket and (summary["reference"] or summary["story"]):
        from ..ops import backup
        _doing(tracker, "library-backup")
        if not library_restored:                          # the disk holds the image's older copies: sent up, they
            log("  library backup: new files only (the library restore failed)")   # would replace newer ones
        try:
            backup.sync_library(bucket, "backup", log=lambda *_: None, only_missing=not library_restored)
        except Exception as exc:
            log(f"  library backup failed: {type(exc).__name__}: {str(exc)[:120]}")
    return summary
