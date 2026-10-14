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
        self.storage, self.fail_keys, self.runs, self.requests = storage, set(fail_keys), [], []

    def run_job(self, request):
        name = request.name if hasattr(request, "name") else request["name"]
        uri = request.overrides.container_overrides[0].env[0].value if hasattr(request, "overrides") \
            else request["manifest"]
        self.runs.append((name, uri))
        self.requests.append(request)
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


def test_cloud_run_job_gets_the_configured_timeout(tmp_path):
    pytest.importorskip("google.cloud.run_v2")
    storage = FakeStorage()
    jobs = FakeJobs(storage)
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "job": "nraiorbit-lipsync",
                       "timeout_seconds": 21600}}
    sync = CloudRunLipSync(tmp_path, cfg, {"cloud_run_gpu": {"per_hour": 1.2}},
                           SpendLedger(tmp_path / "cost.jsonl", None), storage, jobs)
    v, a = _clip(tmp_path, "a", "red")
    sync.apply(v, a, tmp_path / "out.mp4")
    assert jobs.requests[0].overrides.timeout == dt.timedelta(hours=6)    # not the job's deployed 1 h


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


class FakeVertex:
    """A Vertex AI REST session: creating the custom job runs the fake worker; polling reports its state."""

    def __init__(self, storage, quota_error=False):
        self.worker, self.quota_error, self.created, self.polls = FakeJobs(storage), quota_error, [], 0

    def post(self, url, json=None):
        if self.quota_error:
            return SimpleNamespace(status_code=429, text="Quota exceeded for custom_model_training_nvidia_l4_gpus")
        self.created.append((url, json))
        env = json["jobSpec"]["workerPoolSpecs"][0]["containerSpec"]["env"]
        self.worker.run_job({"name": url, "manifest": env[0]["value"]})
        return SimpleNamespace(status_code=200, json=lambda: {"name": "projects/1/locations/us-central1/customJobs/7"})

    def get(self, url):
        self.polls += 1
        state = "JOB_STATE_RUNNING" if self.polls == 1 else "JOB_STATE_SUCCEEDED"
        return SimpleNamespace(json=lambda: {"state": state, "startTime": "2026-10-14T04:10:00Z",
                                             "endTime": "2026-10-14T04:19:00Z"})


def test_vertex_runner_uses_the_same_worker_image_on_an_l4(tmp_path):
    storage = FakeStorage()
    vertex = FakeVertex(storage)
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "runner": "vertex", "poll_seconds": 0,
                       "service_account": "sa@film-proj.iam.gserviceaccount.com"}}
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", None)
    sync = CloudRunLipSync(tmp_path, cfg, {"cloud_run_gpu": {"per_hour": 1.0}}, ledger, storage, vertex)
    v, a = _clip(tmp_path, "a", "red")
    out = sync.apply(v, a, tmp_path / "o.mp4")
    assert out.read_bytes() == v.read_bytes()
    url, body = vertex.created[0]
    assert url == "https://us-central1-aiplatform.googleapis.com/v1/projects/film-proj/locations/us-central1/customJobs"
    pool = body["jobSpec"]["workerPoolSpecs"][0]
    assert pool["machineSpec"]["acceleratorType"] == "NVIDIA_L4" and pool["machineSpec"]["acceleratorCount"] == 1
    assert pool["containerSpec"]["imageUri"] == "us-central1-docker.pkg.dev/film-proj/nraiorbit/latentsync:v1"
    assert body["jobSpec"]["serviceAccount"] == "sa@film-proj.iam.gserviceaccount.com"
    rec = [r for r in ledger.records() if r["provider"] == "lipsync"]
    assert rec[0]["seconds"] == 540                                              # from the job's start/end times


def test_vertex_quota_error_says_what_to_request(tmp_path):
    storage = FakeStorage()
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "runner": "vertex", "poll_seconds": 0}}
    sync = CloudRunLipSync(tmp_path, cfg, {}, SpendLedger(tmp_path / "c.jsonl", None), storage,
                           FakeVertex(storage, quota_error=True))
    v, a = _clip(tmp_path, "a", "red")
    with pytest.raises(GoogleAPIError, match="Custom model training Nvidia L4"):
        sync.apply(v, a, tmp_path / "o.mp4")


class FakeEndpointSession:
    """Vertex AI REST for models, endpoints and long-running operations; predict runs the fake worker."""

    def __init__(self, storage, predict_fails=False):
        self.worker, self.predict_fails, self.calls = FakeJobs(storage), predict_fails, []
        self.deployed, self.pending = [], {}

    def _resp(self, code=200, body=None):
        return SimpleNamespace(status_code=code, text=json.dumps(body or {}), json=lambda: body or {})

    def get(self, url):
        self.calls.append(("GET", url))
        if "/models?filter=" in url:
            return self._resp(body={"models": []})
        if "/operations/" in url:                       # a pending deploy finishes on the first poll
            return self._resp(body={"name": url, "done": True, "response": self.pending.pop(url, {})})
        return self._resp(body={"deployedModels": [{"id": d} for d in self.deployed]})

    def post(self, url, json=None):
        self.calls.append(("POST", url))
        if url.endswith("/models:upload"):
            return self._resp(body={"done": True, "response": {"model": "projects/1/locations/us-central1/models/9"}})
        if url.endswith("/endpoints"):
            return self._resp(body={"done": True, "response": {"name": "projects/1/locations/us-central1/endpoints/5"}})
        if url.endswith(":deployModel"):
            self.deployed.append("dm1")
            op = "projects/1/locations/us-central1/operations/77"
            self.pending[f"https://us-central1-aiplatform.googleapis.com/v1/{op}"] = {"deployedModel": {"id": "dm1"}}
            return self._resp(body={"name": op, "done": False})
        if url.endswith(":predict"):
            if self.predict_fails:
                return self._resp(500, {"error": "boom"})
            self.worker.run_job({"name": url, "manifest": json["instances"][0]["manifest"]})
            return self._resp(body={"predictions": [{"queued": 1}]})
        if url.endswith(":undeployModel"):
            self.deployed.remove(json["deployedModelId"])
            return self._resp(body={"done": True, "response": {}})
        raise AssertionError(url)

    def delete(self, url):
        self.calls.append(("DELETE", url))
        return self._resp(body={"done": True})


def _endpoint_provider(tmp_path, storage, session):
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "runner": "vertex_endpoint",
                       "poll_seconds": 0}}
    return CloudRunLipSync(tmp_path, cfg, {"cloud_run_gpu": {"per_hour": 1.5}},
                           SpendLedger(tmp_path / "reports" / "cost.jsonl", None), storage, session)


def test_vertex_endpoint_deploys_runs_and_always_shuts_down(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    storage = FakeStorage()
    session = FakeEndpointSession(storage)
    sync = _endpoint_provider(tmp_path, storage, session)
    v, a = _clip(tmp_path, "a", "red")
    assert sync.apply(v, a, tmp_path / "o.mp4").read_bytes() == v.read_bytes()
    kinds = [(m, u.rsplit("/", 1)[-1]) for m, u in session.calls if m != "GET"]
    assert kinds == [("POST", "models:upload"), ("POST", "endpoints"), ("POST", "5:deployModel"),
                     ("POST", "5:predict"), ("POST", "5:undeployModel"), ("DELETE", "5")]
    assert session.deployed == []                                    # nothing left running
    assert [r["provider"] for r in sync.ledger.records()] == ["lipsync"]

    failing = FakeEndpointSession(storage, predict_fails=True)
    sync2 = _endpoint_provider(tmp_path, storage, failing)
    v2, a2 = _clip(tmp_path, "b", "blue")
    with pytest.raises(GoogleAPIError, match="refused the batch"):
        sync2.apply(v2, a2, tmp_path / "o2.mp4")
    assert failing.deployed == [] and ("DELETE", "https://us-central1-aiplatform.googleapis.com/v1/"
                                       "projects/1/locations/us-central1/endpoints/5") in failing.calls
