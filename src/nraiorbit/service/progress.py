"""What a running execution is doing, written to the bucket while it works, so the control room shows the stage a
film is on now instead of inferring it from plan files that land only when `produce` returns.

Two small records, each always written whole (never read, changed and written back, so two executions cannot race
on one):

    <slug>/reports/progress.json      the film: state, phase (a pipeline stage or a runner step), its last log line.
                                      Also kept in the project's reports/, so the normal backup carries it.
    queue/running/<execution>.json    the execution: what it is doing (library-restore, reference, story, idle,
                                      library-backup), on which film and phase. Removed when the tick exits.

The page trusts a record only while Cloud Run lists its execution as running; a record that a killed execution left
behind is swept by a later tick once it is older than any run can be. Every upload fails soft and is logged once per
tick: progress reporting must never break a run.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path

RUNNING = "queue/running/"
THROTTLE_S = 15.0            # a line-only update goes up at most this often; a phase change goes up at once
STALE_HOURS = 7.0            # longer than the job's time limit: a running record this old was left behind
TIMEOUT_S = 10               # a slow bucket must not hold the film up for the client's default minute


def stamp(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="seconds")


def state_head(state: str) -> str:
    """done, awaiting, blocked or failed: the produce state without its gate or reason."""
    return str(state).split(":", 1)[0]


class Tracker:
    """One execution's running record. `uploader(path, text)`, `remover(path)` and `lister(prefix)` (returning
    {path: text}) default to the storage client on `bucket`; tests pass dict-backed fakes and a `clock`."""

    def __init__(self, bucket, execution=None, uploader=None, remover=None, clock=None, log=print, lister=None):
        self.bucket = bucket
        self.execution = execution or os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{os.getpid()}"
        self.uploader = uploader or self._upload
        self.remover = remover or self._remove
        self.lister = lister or self._list
        self.clock = clock or time.time
        self.log = log
        self.started = stamp(self.clock())
        self.path = f"{RUNNING}{self.execution}.json"
        self.record = None
        self._handle = None
        self._warned = self._swept = self._closed = False

    def start(self, doing: str, **fields) -> None:
        """What the execution is on now (a reference's url, a story's slug); the whole record is replaced."""
        if not self._swept:
            self._swept = True
            self._sweep()
        self.record = {"execution": self.execution, "started": self.started, "updated": stamp(self.clock()),
                       "doing": doing, **fields}
        self.send(self.path, self.record)

    def film(self, slug: str, root: Path) -> "FilmProgress":
        return FilmProgress(self, slug, Path(root))

    def touch(self, **fields) -> None:
        """The film's phase and last line onto the running record, sent whenever the film's own record is."""
        if self.record is None or self._closed:
            return
        self.record.update(fields, updated=stamp(self.clock()))
        self.send(self.path, self.record)

    def close(self) -> None:
        """Remove the running record; a second call does nothing."""
        if self._closed:
            return
        self._closed = True
        if self.record is not None and self.bucket:
            try:
                self.remover(self.path)
            except Exception as exc:
                self.failed(exc)

    def send(self, path: str, record: dict) -> None:
        if not self.bucket:
            return
        try:
            self.uploader(path, json.dumps(record, indent=1, ensure_ascii=False))
        except Exception as exc:
            self.failed(exc)

    def failed(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            self.log(f"  progress not recorded (the run goes on): {type(exc).__name__}: {str(exc)[:120]}")

    def _sweep(self) -> None:
        """Running records older than any run can be: their execution was killed before its `finally` ran."""
        if not self.bucket:
            return
        try:
            now = self.clock()
            for path, text in self.lister(RUNNING).items():
                try:
                    updated = datetime.datetime.fromisoformat(json.loads(text)["updated"])
                except (ValueError, KeyError, TypeError):
                    continue
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=datetime.timezone.utc)
                if now - updated.timestamp() > STALE_HOURS * 3600:
                    self.remover(path)
        except Exception:
            pass

    # the storage client, made once per tick (google-cloud-storage, never a gcloud process per line)

    def _blobs(self):
        if self._handle is None:
            from ..release import gcs
            self._handle = gcs.client().bucket(self.bucket)
        return self._handle

    def _upload(self, path: str, text: str) -> None:
        # one attempt: the next update replaces this record whole, so a retry would only hold the film up
        self._blobs().blob(path).upload_from_string(text, content_type="application/json", timeout=TIMEOUT_S,
                                                    retry=None)

    def _remove(self, path: str) -> None:
        from google.api_core.exceptions import NotFound
        try:
            self._blobs().blob(path).delete(timeout=TIMEOUT_S)
        except NotFound:
            pass

    def _list(self, prefix: str) -> dict:
        return {b.name: b.download_as_text(timeout=TIMEOUT_S)
                for b in self._blobs().list_blobs(prefix=prefix, timeout=TIMEOUT_S)}


class FilmProgress:
    """One film's progress record while `produce` drives it: a phase goes up at once, log lines at most every
    THROTTLE_S, and the last state when it finishes."""

    def __init__(self, tracker: Tracker, slug: str, root: Path):
        self.tracker, self.root = tracker, root
        self.path = f"{slug}/reports/progress.json"
        now = tracker.clock()
        self.record = {"slug": slug, "execution": tracker.execution, "state": "running", "phase": None,
                       "phase_started": None, "started": stamp(now), "updated": stamp(now), "last_line": "",
                       "lines": 0}
        self._sent = now

    def phase(self, name: str) -> None:
        now = self.tracker.clock()
        self.record.update(phase=name, phase_started=stamp(now), updated=stamp(now))
        self.flush(now)

    def line(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        now = self.tracker.clock()
        self.record.update(last_line=text[:200], lines=self.record["lines"] + 1, updated=stamp(now))
        if now - self._sent >= THROTTLE_S:
            self.flush(now)

    def finish(self, state: str) -> None:
        now = self.tracker.clock()
        self.record.update(state=state_head(state), updated=stamp(now))
        self.flush(now)

    def flush(self, now: float) -> None:
        self._sent = now
        try:
            (self.root / "reports").mkdir(parents=True, exist_ok=True)
            (self.root / "reports" / "progress.json").write_text(
                json.dumps(self.record, indent=1, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:                          # a full disk or a line that will not encode: never the run's end
            self.tracker.failed(exc)
        self.tracker.send(self.path, self.record)
        self.tracker.touch(phase=self.record["phase"], last_line=self.record["last_line"])
