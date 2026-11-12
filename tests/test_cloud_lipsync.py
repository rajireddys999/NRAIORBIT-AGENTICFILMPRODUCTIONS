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
        self.deployed, self.pending, self.bodies = [], {}, []

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
        self.bodies.append((url, json))
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


def test_keep_original_remembers_failures_and_does_not_resend(tmp_path):
    storage = FakeStorage()
    v1, a1 = _clip(tmp_path, "a", "red")
    v2, a2 = _clip(tmp_path, "b", "blue")
    probe, _ = _provider(tmp_path, storage, None)
    jobs = FakeJobs(storage, fail_keys={probe.key(v2, a2)})
    sync, ledger = _provider(tmp_path, storage, jobs)
    sync.on_failure = "keep_original"
    outs = sync.apply_many([(v1, a1, tmp_path / "o1.mp4"), (v2, a2, tmp_path / "o2.mp4")])
    assert outs[0] == tmp_path / "o1.mp4" and outs[0].exists() and outs[1] is None   # no error: the film goes on
    assert "CUDA out of memory" in sync.failure(v2, a2) and sync.failure(v1, a1) is None
    runs = len(jobs.runs)
    assert sync.apply_many([(v2, a2, tmp_path / "o2.mp4")]) == [None] and len(jobs.runs) == runs   # not sent again
    assert len([r for r in ledger.records() if r["provider"] == "lipsync"]) == 1
    jobs.fail_keys.clear()
    sync.retry_failed = True                                       # lipsync.retry_failed: try it once more
    assert sync.apply_many([(v2, a2, tmp_path / "o2.mp4")])[0].exists() and len(jobs.runs) == runs + 1


def test_batches_share_one_endpoint_and_the_image_tag_picks_the_build(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    storage = FakeStorage()
    session = FakeEndpointSession(storage)
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "runner": "vertex_endpoint", "poll_seconds": 0,
                       "image_tag": "v2", "batch_max_shots": 2}}
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", None)
    sync = CloudRunLipSync(tmp_path, cfg, {"vertex_endpoint_gpu": {"per_hour": 1.0, "startup_minutes": 30,
                                                                   "minutes_per_clip": 5}}, ledger, storage, session)
    assert sync.image.endswith("/nraiorbit/latentsync:v2") and sync.batch_max == 2
    shots = [_clip(tmp_path, n, c) for n, c in (("a", "red"), ("b", "blue"), ("c", "green"))]
    outs = sync.apply_many([(v, a, tmp_path / "out" / f"{v.stem}.mp4") for v, a in shots])
    assert all(o is not None and o.exists() for o in outs)
    kinds = [u.rsplit("/", 1)[-1] for m, u in session.calls if m != "GET"]
    assert kinds.count("5:deployModel") == 1 and kinds.count("5:predict") == 2       # two batches, one deploy
    assert kinds.count("5:undeployModel") == 1 and kinds.count("5") == 1 and session.deployed == []
    assert len(session.worker.runs) == 2 and [r["shots"] for r in ledger.records()] == [2, 1]
    # the next batch of the same call is checked without the start-up wait, the first with it
    assert [r["usd"] >= 0 for r in ledger.records()] == [True, True]


class StoppedJobs(FakeJobs):
    """A worker that finishes the first shot, then is stopped before writing the status file."""

    def run_job(self, request):
        name = request.name if hasattr(request, "name") else request["name"]
        uri = request.overrides.container_overrides[0].env[0].value if hasattr(request, "overrides") \
            else request["manifest"]
        path = uri.removeprefix("gs://")
        manifest = json.loads(self.storage.store[path])
        first = manifest["items"][0]
        self.storage.store[first["out"].removeprefix("gs://")] = self.storage.store[first["video"].removeprefix("gs://")]
        self.runs.append((name, uri))
        raise TimeoutError("the run timed out")


def test_a_run_cut_short_keeps_the_finished_shots(tmp_path):
    storage = FakeStorage()
    jobs = StoppedJobs(storage)
    sync, ledger = _provider(tmp_path, storage, jobs)
    v1, a1 = _clip(tmp_path, "a", "red")
    v2, a2 = _clip(tmp_path, "b", "blue")
    with pytest.raises(GoogleAPIError, match=r"1 of 2 shots of this batch were finished and kept"):
        sync.apply_many([(v1, a1, tmp_path / "o1.mp4"), (v2, a2, tmp_path / "o2.mp4")])
    assert sync.cached(v1, a1) is not None and sync.cached(v2, a2) is None
    assert [r.get("note") for r in ledger.records()] == ["run failed"]              # the GPU time still counts
    good = FakeJobs(storage)
    again, _ = _provider(tmp_path, storage, good)
    outs = again.apply_many([(v1, a1, tmp_path / "o1.mp4"), (v2, a2, tmp_path / "o2.mp4")])
    assert all(o.exists() for o in outs) and len(good.runs) == 1
    manifest = json.loads(storage.store[good.runs[0][1].removeprefix("gs://")])
    assert [i["key"] for i in manifest["items"]] == [again.key(v2, a2)]             # only the shot still missing


class LeftUpSession(FakeEndpointSession):
    """Vertex lists an endpoint of ours from days ago that nobody closed."""

    def get(self, url):
        if url.endswith("/endpoints"):
            self.calls.append(("GET", url))
            return self._resp(body={"endpoints": [{"name": "projects/1/locations/us-central1/endpoints/9",
                                                   "displayName": "nraiorbit-lipsync-1760000000",
                                                   "createTime": "2026-10-01T00:00:00Z"}]})
        return super().get(url)


def test_an_endpoint_left_up_stops_the_run_before_spending(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    storage = FakeStorage()
    session = LeftUpSession(storage)
    sync = _endpoint_provider(tmp_path, storage, session)
    v, a = _clip(tmp_path, "a", "red")
    with pytest.raises(GoogleAPIError, match=r"still up from an earlier run.*endpoints/9.*Delete it"):
        sync.apply(v, a, tmp_path / "o.mp4")
    posts = [u.rsplit("/", 1)[-1] for m, u in session.calls if m == "POST"]
    assert posts == [] and not any(m == "DELETE" for m, _ in session.calls)       # nothing made, nothing deleted


def test_musetalk_is_a_second_engine_that_never_mixes_with_latentsync(tmp_path, monkeypatch):
    import time as _time

    from nraiorbit.providers.base import get_provider
    from nraiorbit.providers.cloud_lipsync import MuseTalkCloud

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    storage = FakeStorage()
    session = FakeEndpointSession(storage)
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "runner": "vertex_endpoint", "poll_seconds": 0,
                       "image_tag": "v1"}}
    pricing = {"vertex_endpoint_gpu": {"per_hour": 1.0, "startup_minutes": 50, "minutes_per_clip": 6.5,
                                       "engines": {"musetalk": {"minutes_per_clip": 1.5}}}}
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", None)
    sync = get_provider("lipsync", "musetalk_cloudrun", project_root=tmp_path, cfg=cfg, pricing=pricing, ledger=ledger,
                        storage_client=storage, jobs_client=session)
    assert isinstance(sync, MuseTalkCloud) and sync.engine == "musetalk" and sync.label == "MuseTalk"
    assert sync.image.endswith("/nraiorbit/musetalk:v1") and sync.price["minutes_per_clip"] == 1.5
    latent = CloudRunLipSync(tmp_path, cfg, pricing, ledger, storage, session)
    assert latent.image.endswith("/nraiorbit/latentsync:v1") and latent.price["minutes_per_clip"] == 6.5
    v, a = _clip(tmp_path, "a", "red")
    assert sync.key(v, a) != latent.key(v, a)                                   # separate cache entries
    assert sync.apply(v, a, tmp_path / "o.mp4").read_bytes() == v.read_bytes()
    assert sync.cached(v, a) is not None and latent.cached(v, a) is None        # a MuseTalk result is never LatentSync's
    manifest = json.loads(storage.store[session.worker.runs[0][1].removeprefix("gs://")])
    assert manifest["engine"] == "musetalk" and manifest["fp16"] is True
    uploads = [b for u, b in session.bodies if u.endswith("/models:upload")]
    assert uploads[0]["model"]["displayName"] == "nraiorbit-lipsync-musetalk-v1"   # not LatentSync's nraiorbit-lipsync-v2
    assert uploads[0]["model"]["containerSpec"]["imageUri"].endswith("/nraiorbit/musetalk:v1")
    v2, a2 = _clip(tmp_path, "b", "blue")
    session.worker.fail_keys.add(sync.key(v2, a2))
    with pytest.raises(GoogleAPIError, match=r"lip-sync failed for 1 of 1 shots \(MuseTalk 1.6\)"):
        sync.apply(v2, a2, tmp_path / "o2.mp4")
    assert session.deployed == []                                               # the endpoint is closed either way
    with pytest.raises(GoogleAPIError, match="lipsync.engine must be one of latentsync, musetalk"):
        CloudRunLipSync(tmp_path, {"lipsync": {**cfg["lipsync"], "engine": "wav2lip"}}, pricing, ledger, storage, session)


class FlakySession(FakeEndpointSession):
    """The network drops twice while the model upload is polled, then comes back."""

    def __init__(self, storage):
        super().__init__(storage)
        self.drops = 0

    def post(self, url, json=None):
        if url.endswith("/models:upload"):                # a pending upload, polled through _wait_op
            self.calls.append(("POST", url))
            self.bodies.append((url, json))
            op = "projects/1/locations/us-central1/operations/41"
            self.pending[f"https://us-central1-aiplatform.googleapis.com/v1/{op}"] = \
                {"model": "projects/1/locations/us-central1/models/9"}
            return self._resp(body={"name": op, "done": False})
        return super().post(url, json)

    def get(self, url):
        if "/operations/41" in url and self.drops < 2:
            self.drops += 1
            raise TimeoutError("The read operation timed out")
        return super().get(url)


def test_a_dropped_poll_does_not_end_the_run(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    storage = FakeStorage()
    session = FlakySession(storage)
    sync = _endpoint_provider(tmp_path, storage, session)
    v, a = _clip(tmp_path, "a", "red")
    assert sync.apply(v, a, tmp_path / "o.mp4").read_bytes() == v.read_bytes()
    assert session.drops == 2 and session.deployed == []                        # went on after the drops, closed

    class Dead(FlakySession):
        def get(self, url):
            if "/operations/41" in url:
                raise ConnectionError("no route to host")
            return super().get(url)

    dead = Dead(storage)
    sync2 = _endpoint_provider(tmp_path, storage, dead)
    v2, a2 = _clip(tmp_path, "b", "blue")
    with pytest.raises(GoogleAPIError, match="could not reach Vertex AI while registering the worker model"):
        sync2.apply(v2, a2, tmp_path / "o2.mp4")
    assert not any(m == "POST" and u.endswith("/endpoints") for m, u in dead.calls)   # nothing was opened or billed
