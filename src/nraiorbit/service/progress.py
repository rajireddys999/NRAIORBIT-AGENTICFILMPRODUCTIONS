"""What a running execution is doing, written to the bucket while it works, so the control room shows the stage a
film is on now instead of inferring it from plan files that land only when `produce` returns.

Two small records, each always written whole (never read, changed and written back, so two executions cannot race
on one):

    <slug>/reports/progress.json      the film: state, phase (a pipeline stage or a runner step), its last log line,
                                      the claim it runs under and its heartbeat. Also kept in the project's
                                      reports/ (the backup leaves it out: only the run writes the bucket's copy).
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
HEARTBEAT_S = 120.0          # a film's record is touched this often while its run lives, silent steps included
STALL_S = 15 * 60            # a film whose run has not beaten for this long is dead: the next tick takes it over
FENCE_S = 10 * 60            # a run whose beats have not landed for this long pays for nothing more (LostContact)
BEAT_TRIES = 3               # a beat that does not land is sent again, BEAT_RETRY_S apart
BEAT_RETRY_S = 10.0


class LostContact(RuntimeError):
    """The bucket has not taken this run's heartbeat for FENCE_S: a later tick may judge the run dead and take the
    film over, so this run stops before its next paid call rather than pay alongside the new one."""
MIN_GAP_S = 1.1              # Cloud Storage takes one write a second to the same object; a faster one is refused
HISTORY = 8                  # the film record keeps its last few finished phases, for the page's "what happened"
WORKED_S = 5.0               # a phase shorter than this found its work done already (a stage on disk): not news
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

    def __init__(self, bucket, execution=None, uploader=None, remover=None, clock=None, log=print, lister=None,
                 sleep=None):
        self.bucket = bucket
        self.execution = execution or os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{os.getpid()}"
        self.uploader = uploader or self._upload
        self.remover = remover or self._remove
        self.lister = lister or self._list
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self._last: dict[str, float] = {}                # path -> when it was last written
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

    def film(self, slug: str, root: Path, claim: str | None = None) -> "FilmProgress":
        return FilmProgress(self, slug, Path(root), claim)

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
            self._space(self.path)
            try:
                self.remover(self.path)
            except Exception as exc:
                self.failed(exc)
            finally:
                self._last[self.path] = self.clock()

    def send(self, path: str, record: dict) -> bool:
        """Whether the record landed."""
        if not self.bucket:
            return False
        self._space(path)
        try:
            self.uploader(path, json.dumps(record, indent=1, ensure_ascii=False))
        except Exception as exc:
            self.failed(exc)
            return False
        finally:
            self._last[path] = self.clock()              # the gap runs from when this change landed
        return True

    def _space(self, path: str) -> None:
        """Cloud Storage takes one change a second to the same object and refuses a faster one with 429, so a
        change that comes sooner waits out the rest of that second. Only phase changes come this close together
        (lines go up every THROTTLE_S), so the wait is rare and never more than MIN_GAP_S; no update is dropped."""
        wait = MIN_GAP_S - (self.clock() - self._last.get(path, float("-inf")))
        if wait > 0.05:                                  # MIN_GAP_S keeps a tenth of a second spare; skip a sliver
            self.sleep(min(wait, MIN_GAP_S))

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

    def __init__(self, tracker: Tracker, slug: str, root: Path, claim: str | None = None):
        self.tracker, self.root = tracker, root
        self.path = f"{slug}/reports/progress.json"
        now = tracker.clock()
        self.record = {"slug": slug, "execution": tracker.execution, "state": "running", "phase": None,
                       "phase_started": None, "started": stamp(now), "updated": stamp(now), "last_line": "",
                       "lines": 0, "beat": stamp(now), "claim": claim, "history": []}
        self._sent = now
        import threading
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread, self.landed = None, None            # the beat thread; when a beat last reached the bucket
        self.lost = False                                 # out of touch past FENCE_S: never beats or pays again

    def start_beat(self, interval: float = HEARTBEAT_S) -> None:
        """Touch the record now and every `interval` seconds until stop_beat(), from a thread of its own: a step
        that logs nothing for half an hour (lip-sync polling) still shows its run is alive, and a run the platform
        killed (preempted) stops beating, so a later tick takes the film over (queue.claim_dead). It beats on after
        finish(), through the backup, until the tick has written the film's row."""
        import threading

        def beat():
            while not self._stop.wait(interval):
                for _ in range(BEAT_TRIES):
                    try:
                        now = self.tracker.clock()
                        if self._out_of_touch(now):       # a later tick may own the film by now: never write again
                            return
                        self.record["beat"] = stamp(now)
                        if self.flush(now):
                            self.landed = now
                            break
                    except Exception as exc:              # a beat that fails is sent again, never the thread's end
                        self.tracker.failed(exc)
                    if self._stop.wait(BEAT_RETRY_S):
                        return
        now = self.tracker.clock()
        if self.flush(now):                               # this claim's record is in the bucket from the start
            self.landed = now
        self.landed = self.landed or now                  # the fence counts from here even if that first one failed
        self._thread = threading.Thread(target=beat, name="film-heartbeat", daemon=True)
        self._thread.start()

    def stop_beat(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)                 # a beat in flight lands before the tick closes its records

    def _out_of_touch(self, now: float) -> bool:
        """Whether this run's beats have not reached the bucket for FENCE_S; once so, for good (`lost`): a beat that
        lands later must not make a run that a later tick may have taken over (STALL_S) look alive again."""
        if not self.lost and self._thread is not None and self.landed is not None and now - self.landed > FENCE_S:
            self.lost = True
            self.tracker.log(f"  this run has not reached the bucket for {(now - self.landed) / 60:.0f} minutes: "
                             "it pays for nothing more and leaves the film to the next tick")
        return self.lost

    def fence(self) -> None:
        """Called before every paid call (SpendLedger.check) and before the YouTube upload: raises LostContact once
        this run is out of touch (_out_of_touch), before a later tick could judge it dead (STALL_S) and pay for the
        same work."""
        if self._out_of_touch(self.tracker.clock()):
            raise LostContact("this run lost touch with the bucket for over 10 minutes; it stops before paying for "
                              "more, so no second run pays for the same work. Retry once the bucket answers.")

    def phase(self, name: str) -> None:
        now = self.tracker.clock()
        self.record.update(history=self._ended(now), phase=name, phase_started=stamp(now), updated=stamp(now))
        self.flush(now)

    def _ended(self, now: float) -> list:
        """The record's short history (newest last) with the phase under way over: a phase that did work (WORKED_S
        or longer: a stage already on disk passes in a moment) replaces its own earlier entry, for the page."""
        history, phase = list(self.record.get("history") or []), self.record.get("phase")
        started = datetime.datetime.fromisoformat(self.record["phase_started"]).timestamp() \
            if self.record.get("phase_started") else now
        if not phase or now - started < WORKED_S:
            return history
        return [*(h for h in history if h.get("phase") != phase), {"phase": phase, "done": stamp(now)}][-HISTORY:]

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
        history = self._ended(now) if state_head(state) == "done" else self.record.get("history", [])
        self.record.update(state=state_head(state), updated=stamp(now), beat=stamp(now), history=history)
        # (a run that stopped at a gate or on a problem did not finish its last phase; one that is done did)
        self.flush(now)

    def flush(self, now: float) -> bool:
        """Whether the record reached the bucket."""
        with self._lock:                                  # the heartbeat thread and the run never write at once
            return self._flush(now)

    def _flush(self, now: float) -> bool:
        self._sent = now
        try:
            (self.root / "reports").mkdir(parents=True, exist_ok=True)
            part = self.root / "reports" / "progress.json.part"
            part.write_text(json.dumps(self.record, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(part, self.root / "reports" / "progress.json")   # never read half-written
        except Exception as exc:                          # a full disk or a line that will not encode: never the run's end
            self.tracker.failed(exc)
        if self.lost:                                     # the bucket's record may be the run that took over: its own
            return False
        landed = self.tracker.send(self.path, self.record)
        if landed:
            self.landed = now
        self.tracker.touch(phase=self.record["phase"], last_line=self.record["last_line"])
        return landed
