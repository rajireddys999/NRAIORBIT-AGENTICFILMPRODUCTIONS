"""Lip sync on a Cloud Run GPU job: batching, staging, caching, ledger and failures (fake GCS + fake job)."""

import datetime as dt
import json
import shutil
from types import SimpleNamespace

import pytest

from nraiorbit.costing import BudgetExceeded, SpendLedger
from nraiorbit.post import ffmpeg
from nraiorbit.providers.cloud_lipsync import CloudRunLipSync
from nraiorbit.providers.google_ai import GoogleAPIError

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")


class FakeBlob:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def upload_from_filename(self, filename, content_type=None):
        self.store[self.path] = open(filename, "rb").read()

    def upload_from_string(self, data, content_type=None):
        self.store[self.path] = data.encode() if isinstance(data, str) else data

    def download_to_filename(self, filename):
        open(filename, "wb").write(self.store[self.path])

    def download_as_text(self):
        return self.store[self.path].decode()

    def exists(self):
        return self.path in self.store


class FakeStorage:
    def __init__(self):
        self.store = {}

    def bucket(self, name):
        return SimpleNamespace(blob=lambda path: FakeBlob(self.store, f"{name}/{path}"))


class FakeJobs:
    """Plays the worker: copies each staged video to its output and writes the status file."""

    def __init__(self, storage, fail_keys=()):
        self.storage, self.fail_keys, self.runs = storage, set(fail_keys), []

    def run_job(self, request):
        name = request.name if hasattr(request, "name") else request["name"]
        uri = request.overrides.container_overrides[0].env[0].value if hasattr(request, "overrides") \
            else request["manifest"]
        self.runs.append((name, uri))
        path = uri.removeprefix("gs://")
        manifest = json.loads(self.storage.store[path])
        results = []
        for item in manifest["items"]:
            ok = item["key"] not in self.fail_keys
            if ok:
                self.storage.store[item["out"].removeprefix("gs://")] = \
                    self.storage.store[item["video"].removeprefix("gs://")]
            results.append({"key": item["key"], "ok": ok, **({} if ok else {"error": "CUDA out of memory"})})
        self.storage.store[path.removesuffix(".json") + ".done.json"] = json.dumps({"version": "1.6",
                                                                                     "results": results}).encode()
        start = dt.datetime(2026, 10, 14, 3, 0, 0, tzinfo=dt.timezone.utc)
        return SimpleNamespace(result=lambda timeout=None: SimpleNamespace(
            start_time=start, completion_time=start + dt.timedelta(minutes=12)))


def _clip(tmp_path, name, color):
    path = tmp_path / f"{name}.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=24:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(path)])
    wav = tmp_path / f"{name}.wav"
    ffmpeg.run(["-f", "lavfi", "-t", "2", "-i", "sine=f=220:r=48000", str(wav)])
    return path, wav


def _provider(tmp_path, storage, jobs, budget=None):
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "job": "nraiorbit-lipsync"}}
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", budget)
    return CloudRunLipSync(tmp_path, cfg, {"cloud_run_gpu": {"per_hour": 1.2}}, ledger, storage, jobs), ledger


def test_batch_runs_once_caches_and_logs_gpu_time(tmp_path):
    storage = FakeStorage()
    jobs = FakeJobs(storage)
    sync, ledger = _provider(tmp_path, storage, jobs)
    shots = [_clip(tmp_path, n, c) for n, c in (("a", "red"), ("b", "blue"), ("c", "green"))]
    outs = sync.apply_many([(v, a, tmp_path / "out" / f"{v.stem}_raw.mp4") for v, a in shots])
    assert len(jobs.runs) == 1 and jobs.runs[0][0] == "projects/film-proj/locations/us-central1/jobs/nraiorbit-lipsync"
    assert [o.read_bytes() for o in outs] == [v.read_bytes() for v, _ in shots]       # the fake job echoes video
    staged = [k for k in storage.store if k.startswith("film-media/lipsync/")]
    assert sum(k.endswith("/video.mp4") for k in staged) == 3 and sum(k.endswith("/audio.wav") for k in staged) == 3
    rec = [r for r in ledger.records() if r["provider"] == "lipsync"]
    assert len(rec) == 1 and rec[0]["seconds"] == 720 and rec[0]["usd"] == pytest.approx(1.2 * 720 / 3600)
    again = sync.apply_many([(v, a, tmp_path / "again" / f"{v.stem}.mp4") for v, a in shots])
    assert len(jobs.runs) == 1 and all(p.exists() for p in again)                    # cached: no new GPU run
    assert len([r for r in ledger.records() if r["provider"] == "lipsync"]) == 1


def test_budget_stops_before_any_gpu_time(tmp_path):
    storage = FakeStorage()
    jobs = FakeJobs(storage)
    sync, _ = _provider(tmp_path, storage, jobs, budget=0.05)
    v, a = _clip(tmp_path, "a", "red")
    with pytest.raises(BudgetExceeded):
        sync.apply(v, a, tmp_path / "out.mp4")
    assert jobs.runs == [] and storage.store == {}


def test_failed_shot_is_reported_good_ones_are_kept(tmp_path):
    storage = FakeStorage()
    v1, a1 = _clip(tmp_path, "a", "red")
    v2, a2 = _clip(tmp_path, "b", "blue")
    probe, _ = _provider(tmp_path, storage, None)
    jobs = FakeJobs(storage, fail_keys={probe.key(v2, a2)})
    sync, ledger = _provider(tmp_path, storage, jobs)
    with pytest.raises(GoogleAPIError, match="(?s)lip-sync failed for 1 of 2 shots.*CUDA out of memory"):
        sync.apply_many([(v1, a1, tmp_path / "o1.mp4"), (v2, a2, tmp_path / "o2.mp4")])
    assert (tmp_path / "cache" / "lipsync" / f"{sync.key(v1, a1)}.mp4").exists()     # the good shot is kept
    assert len([r for r in ledger.records() if r["provider"] == "lipsync"]) == 1      # GPU time still logged
    jobs.fail_keys.clear()
    sync.apply_many([(v1, a1, tmp_path / "o1.mp4"), (v2, a2, tmp_path / "o2.mp4")])
    manifest = json.loads(storage.store[jobs.runs[-1][1].removeprefix("gs://")])
    assert [i["key"] for i in manifest["items"]] == [sync.key(v2, a2)]                 # only the failed one again


def test_missing_status_or_job_error_stops_cleanly(tmp_path):
    storage = FakeStorage()

    class Broken:
        def run_job(self, request):
            raise RuntimeError("404 Job nraiorbit-lipsync not found")

    sync, _ = _provider(tmp_path, storage, Broken())
    v, a = _clip(tmp_path, "a", "red")
    with pytest.raises(GoogleAPIError, match="setup.sh"):
        sync.apply(v, a, tmp_path / "o.mp4")
    shutil.rmtree(tmp_path / "reports", ignore_errors=True)
