"""Lip sync on your own Google Cloud GPU: LatentSync as a Cloud Run job with one NVIDIA L4 (deploy/lipsync/), or
the same worker image on Vertex AI (lipsync.runner: vertex_endpoint, the route this project uses today).

    providers.lipsync: latentsync_cloudrun
    lipsync: {job: nraiorbit-lipsync, region: us-central1, bucket: <project>-media, steps: 20, guidance: 1.5,
              runner: vertex_endpoint, image_tag: v2, batch_max_shots: 24}

The shots of a render go to the GPU in batches (lipsync.batch_max_shots a batch; 0 = all at once): clips and their
dialogue audio are staged in your private bucket under lipsync/<run>/, the worker writes the synced clips back with a
status file, and each batch's results are cached in cache/lipsync/ by content as soon as it is done, so a run that
ends early keeps what it paid for and a re-render never pays twice. On the Vertex endpoint runner one endpoint serves
every batch of a call and is always undeployed and deleted at the end; if an endpoint of ours is found still up from
an earlier run, the run stops before spending and names it for the owner to delete. The GPU time of every batch is
checked against the budget before it starts and logged to reports/cost.jsonl.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from ..costing import SpendLedger, gpu_price
from .base import register
from .google_ai import GoogleAPIError

STORAGE_FACTORY = None          # tests inject fakes here
JOBS_FACTORY = None
__all__ = ["CloudRunLipSync", "gpu_price"]


def _credentials():
    """The service account in GOOGLE_APPLICATION_CREDENTIALS_JSON, else None (Application Default Credentials)."""
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
    if not raw:
        return None
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/cloud-platform"])


@register("lipsync", "latentsync_cloudrun")
class CloudRunLipSync:
    def __init__(self, project_root: Path, cfg: dict, pricing: dict, ledger: SpendLedger,
                 storage_client=None, jobs_client=None):
        ls = cfg.get("lipsync") or {}
        self.project = ls.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not self.project:
            raise GoogleAPIError("lip-sync: set GOOGLE_CLOUD_PROJECT (or lipsync.project) for the Cloud Run job.")
        self.region = ls.get("region", "us-central1")
        self.job = ls.get("job", "nraiorbit-lipsync")
        self.bucket = ls.get("bucket") or f"{self.project}-media"
        self.steps, self.guidance = int(ls.get("steps", 20)), float(ls.get("guidance", 1.5))
        self.timeout_s = int(ls.get("timeout_seconds", 3600))
        # runner: cloudrun (a Cloud Run job; needs Cloud Run's non-zonal GPU quota), vertex (a Vertex AI custom job;
        # needs custom-training GPU quota) or vertex_endpoint (a temporary Vertex AI endpoint on one L4; uses the
        # custom-model-serving GPU quota new projects have). All run the same worker image.
        self.runner = ls.get("runner", "cloudrun")
        tag = str(ls.get("image_tag") or "v1")               # which build of the worker image (deploy/lipsync)
        self.image = ls.get("image") or (f"{self.region}-docker.pkg.dev/{self.project}/nraiorbit/latentsync:{tag}")
        self.machine = ls.get("machine_type", "g2-standard-8")
        self.batch_max = max(0, int(ls.get("batch_max_shots") or 0))   # shots per GPU batch; 0 = all in one
        self._shared: dict | None = None                     # a Vertex endpoint kept up across one call's batches
        self.service_account = ls.get("service_account") or os.environ.get("LIPSYNC_SERVICE_ACCOUNT")
        self.poll_s = float(ls.get("poll_seconds", 20))
        # stop: a shot LatentSync cannot do stops the run (it is retried next time, e.g. after a GPU error);
        # keep_original: that shot keeps its clip, the failure is remembered and the shot is not sent again
        # (lipsync.retry_failed: true sends it again)
        self.on_failure = ls.get("on_failure", "stop")
        self.retry_failed = bool(ls.get("retry_failed", False))
        self.price = gpu_price(pricing, ls.get("runner", "cloudrun"))
        self.cache = project_root / "cache" / "lipsync"
        self.ledger = ledger
        self._storage, self._jobs = storage_client, jobs_client

    # -- clients (made on first use, so cached results need no sign-in)

    def storage(self):
        if self._storage is None:
            if STORAGE_FACTORY is not None:
                self._storage = STORAGE_FACTORY()
            else:
                from google.cloud import storage

                self._storage = storage.Client(project=self.project, credentials=_credentials())
        return self._storage

    def jobs(self):
        if self._jobs is None:
            if JOBS_FACTORY is not None:
                self._jobs = JOBS_FACTORY()
            else:
                from google.cloud import run_v2

                self._jobs = run_v2.JobsClient(credentials=_credentials())
        return self._jobs

    # -- provider API

    def key(self, video: Path, audio: Path) -> str:
        h = hashlib.sha1(json.dumps(["latentsync", self.steps, self.guidance]).encode())
        h.update(video.read_bytes())
        h.update(audio.read_bytes())
        return h.hexdigest()[:16]

    def apply(self, video: Path, audio: Path, out: Path) -> Path | None:
        return self.apply_many([(video, audio, out)])[0]

    def cached(self, video: Path, audio: Path) -> Path | None:
        """The synced clip made before for exactly this video and audio, else None. Nothing is run or paid."""
        path = self.cache / f"{self.key(video, audio)}.mp4"
        return path if path.exists() else None

    def failure(self, video: Path, audio: Path) -> str | None:
        """Why LatentSync could not do this shot (remembered under on_failure: keep_original), else None."""
        marker = self.cache / f"{self.key(video, audio)}.failed.json"
        return json.loads(marker.read_text(encoding="utf-8")).get("error") if marker.exists() else None

    def apply_many(self, items: list[tuple[Path, Path, Path]]) -> list[Path | None]:
        """Synced clips in order; None for a shot LatentSync could not do when on_failure is keep_original."""
        keys = [self.key(v, a) for v, a, _ in items]
        known_bad = set() if self.retry_failed else {k for k in keys if (self.cache / f"{k}.failed.json").exists()}
        todo = {k: (v, a) for k, (v, a, _) in zip(keys, items)
                if not (self.cache / f"{k}.mp4").exists() and k not in known_bad}
        if todo:
            batches = self._batches(todo)
            if len(batches) > 1 and self.runner == "vertex_endpoint":
                with self._endpoint_kept():                  # one deploy for all the batches, closed after
                    for batch in batches:
                        self._run(batch)
            else:
                for batch in batches:
                    self._run(batch)
        outs = []
        for k, (_, _, out) in zip(keys, items):
            if not (self.cache / f"{k}.mp4").exists():
                outs.append(None)                      # failed and remembered (keep_original)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.cache / f"{k}.mp4", out)
            outs.append(out)
        return outs

    def _batches(self, todo: dict) -> list[dict]:
        """The shots to sync in batches of lipsync.batch_max_shots (one batch when 0), in order."""
        keys = list(todo)
        size = self.batch_max or len(keys)
        return [{k: todo[k] for k in keys[i:i + size]} for i in range(0, len(keys), size)] or [todo]

    # -- one job execution for every shot not yet synced

    def _run(self, todo: dict[str, tuple[Path, Path]]) -> None:
        p = self.price
        startup = 0.0 if self._shared and not self._shared.get("start") else p["startup_minutes"]
        estimate = p["per_hour"] * (startup + p["minutes_per_clip"] * len(todo)) / 60
        self.ledger.check(estimate, f"lip-sync of {len(todo)} shots on your L4 GPU ({self.runner})")
        run = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        prefix = f"lipsync/{run}"
        bucket = self.storage().bucket(self.bucket)
        manifest = {"steps": self.steps, "guidance": self.guidance, "items": []}
        for k, (video, audio) in todo.items():
            bucket.blob(f"{prefix}/{k}/video.mp4").upload_from_filename(str(video), content_type="video/mp4")
            bucket.blob(f"{prefix}/{k}/audio.wav").upload_from_filename(str(audio), content_type="audio/wav")
            manifest["items"].append({"key": k, "video": f"gs://{self.bucket}/{prefix}/{k}/video.mp4",
                                      "audio": f"gs://{self.bucket}/{prefix}/{k}/audio.wav",
                                      "out": f"gs://{self.bucket}/{prefix}/{k}/out.mp4"})
        bucket.blob(f"{prefix}/manifest.json").upload_from_string(json.dumps(manifest), content_type="application/json")
        started, execution = time.time(), None
        try:
            execution = self._execute(f"gs://{self.bucket}/{prefix}/manifest.json")
        except GoogleAPIError as e:                      # a run that ends early keeps the shots already finished
            kept = self._keep_finished(bucket, prefix, todo)
            raise GoogleAPIError(f"{e}\n({kept} of {len(todo)} shots of this batch were finished and kept; "
                                 "the rest are made on the next run)") from None
        finally:                                         # GPU time counts toward the budget even when a run fails
            seconds = (_elapsed(execution) if execution is not None else None) or (time.time() - started)
            self.ledger.record("lipsync", f"{len(todo)} shots", p["per_hour"] * seconds / 3600,
                               seconds=round(seconds), shots=len(todo), job=self.job, run=run, runner=self.runner,
                               **({} if execution is not None else {"note": "run failed"}))
        done = bucket.blob(f"{prefix}/manifest.done.json")
        if not done.exists():
            kept = self._keep_finished(bucket, prefix, todo)
            raise GoogleAPIError(f"lip-sync job {self.job} wrote no status for run {run}; read its logs in Cloud Run "
                                 f"(region {self.region}). {kept} of {len(todo)} shots were finished and kept; "
                                 "re-run to make the rest.")
        status = json.loads(done.download_as_text())
        failed = []
        for r in status.get("results", []):
            if r.get("ok"):
                self._keep(bucket, prefix, r["key"])
            else:
                failed.append(r)
        if failed and self.on_failure == "keep_original":
            for r in failed:
                (self.cache / f"{r['key']}.failed.json").write_text(json.dumps(
                    {"error": r.get("error", "")[-3000:], "version": status.get("version"), "run": run}),
                    encoding="utf-8")
        elif failed:
            raise GoogleAPIError(f"lip-sync failed for {len(failed)} of {len(todo)} shots (LatentSync "
                                 f"{status.get('version', '?')}). First error:\n{failed[0].get('error', '')[-1200:]}")

    def _keep(self, bucket, prefix: str, key: str) -> None:
        """One synced clip from the staging area into the cache, whole or not at all."""
        self.cache.mkdir(parents=True, exist_ok=True)
        tmp = self.cache / f".{key}.part"
        bucket.blob(f"{prefix}/{key}/out.mp4").download_to_filename(str(tmp))
        os.replace(tmp, self.cache / f"{key}.mp4")

    def _keep_finished(self, bucket, prefix: str, todo: dict) -> int:
        """After a run that ended without its status file (a timeout, a stopped job): every shot whose synced clip
        the worker had already written is cached, so its GPU minutes are not paid for twice. Returns how many."""
        kept = 0
        for key in todo:
            if (self.cache / f"{key}.mp4").exists():
                kept += 1
                continue
            try:
                if bucket.blob(f"{prefix}/{key}/out.mp4").exists():
                    self._keep(bucket, prefix, key)
                    kept += 1
            except Exception:                            # best effort: the run has failed already
                continue
        return kept

    def _execute(self, manifest_uri: str):
        if self.runner == "vertex":
            return self._execute_vertex(manifest_uri)
        if self.runner == "vertex_endpoint":
            return self._execute_vertex_endpoint(manifest_uri)
        name = f"projects/{self.project}/locations/{self.region}/jobs/{self.job}"
        try:
            from google.cloud import run_v2

            request = run_v2.RunJobRequest(name=name, overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name="MANIFEST", value=manifest_uri)])],
                timeout=datetime.timedelta(seconds=self.timeout_s)))   # else the job's deployed --task-timeout
        except ImportError:                              # fakes in tests take a plain dict
            request = {"name": name, "manifest": manifest_uri}
        try:
            return self.jobs().run_job(request=request).result(timeout=self.timeout_s)
        except Exception as e:
            if isinstance(e, GoogleAPIError):
                raise
            raise GoogleAPIError(f"lip-sync job {name}: {type(e).__name__}: {e}\nIs the job set up "
                                 "(bash deploy/lipsync/setup.sh)? Re-run to resume.") from None


    def _session(self):
        if self._jobs is not None:                       # tests pass a fake session
            return self._jobs
        if JOBS_FACTORY is not None:
            return JOBS_FACTORY()
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds = _credentials() or google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])[0]
        return AuthorizedSession(creds)

    def _execute_vertex(self, manifest_uri: str):
        """A Vertex AI custom job running the worker image on one L4; returns its final state as an execution."""
        base = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{self.project}/locations/{self.region}"
        spec = {"workerPoolSpecs": [{
            "machineSpec": {"machineType": self.machine, "acceleratorType": "NVIDIA_L4", "acceleratorCount": 1},
            "replicaCount": "1", "diskSpec": {"bootDiskType": "pd-ssd", "bootDiskSizeGb": 100},
            "containerSpec": {"imageUri": self.image, "env": [{"name": "MANIFEST", "value": manifest_uri}]}}],
            "scheduling": {"timeout": f"{self.timeout_s}s"}}
        if self.service_account:
            spec["serviceAccount"] = self.service_account
        session = self._session()
        resp = session.post(f"{base}/customJobs", json={"displayName": f"{self.job}-{int(time.time())}",
                                                        "jobSpec": spec})
        if resp.status_code >= 300:
            raise GoogleAPIError(f"lip-sync Vertex job: {resp.status_code} {resp.text[:800]}\nIf this is a quota error, "
                                 "request 'Custom model training Nvidia L4 GPUs per region' in IAM & Admin > Quotas.")
        name = resp.json()["name"]
        deadline = time.time() + self.timeout_s + 1800      # provisioning comes on top of the run timeout
        while True:
            job = session.get(f"https://{self.region}-aiplatform.googleapis.com/v1/{name}").json()
            state = job.get("state", "")
            if state in ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
                break
            if time.time() > deadline:
                raise GoogleAPIError(f"lip-sync Vertex job {name} still {state} after the timeout; check it in the "
                                     "Vertex AI console, then re-run.")
            time.sleep(self.poll_s)
        if state != "JOB_STATE_SUCCEEDED":
            raise GoogleAPIError(f"lip-sync Vertex job {name} ended {state}: {job.get('error', {}).get('message', '')}")
        return SimpleExecution(_ts(job.get("startTime")), _ts(job.get("endTime")))


    # -- Vertex AI endpoint: uses the "custom model serving" GPU quota, which new projects have (training has none)

    def _wait_op(self, session, op: dict, what: str, limit_s: float) -> dict:
        deadline = time.time() + limit_s
        while not op.get("done"):
            if time.time() > deadline:
                raise GoogleAPIError(f"lip-sync: {what} did not finish in {limit_s / 60:.0f} min ({op.get('name')})")
            time.sleep(self.poll_s)
            op = session.get(f"https://{self.region}-aiplatform.googleapis.com/v1/{op['name']}").json()
        if op.get("error"):
            raise GoogleAPIError(f"lip-sync: {what} failed: {op['error'].get('message', op['error'])}")
        return op.get("response", {})

    def _call(self, session, method: str, url: str, what: str, body: dict | None = None) -> dict:
        resp = session.post(url, json=body or {}) if method == "post" else session.delete(url)
        if resp.status_code >= 300:
            raise GoogleAPIError(f"lip-sync: {what}: {resp.status_code} {resp.text[:800]}")
        return resp.json() if resp.text else {}

    def _model(self, session, base: str) -> str:
        """The worker image registered once as a Vertex model (serving mode); reused on every run."""
        display = f"{self.job}-{self.image.rsplit(':', 1)[-1]}"
        found = session.get(f'{base}/models?filter=display_name="{display}"').json().get("models", [])
        if found:
            return found[0]["name"]
        op = self._call(session, "post", f"{base}/models:upload", "registering the worker model", {"model": {
            "displayName": display, "containerSpec": {
                "imageUri": self.image, "command": ["python", "/opt/worker.py", "serve"],
                "ports": [{"containerPort": 8080}], "predictRoute": "/predict", "healthRoute": "/health"}}})
        return self._wait_op(session, op, "registering the worker model", 900)["model"]

    def _vertex_base(self) -> str:
        return f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{self.project}/locations/{self.region}"

    def _left_up(self, session, base: str) -> list[str]:
        """Endpoints of ours (<job>-<time>) older than a whole run plus an hour: left up by a run that was stopped
        before it could close them, and billing since. Nothing is deleted here (that is the owner's call); the run
        stops before spending more and says which ones to delete."""
        try:
            found = session.get(f"{base}/endpoints").json().get("endpoints", [])
        except Exception:
            return []
        cutoff = time.time() - (self.timeout_s + 3600)
        old = []
        for e in found or []:
            name = str(e.get("displayName") or "")
            made = _ts(e.get("createTime"))
            if name.startswith(f"{self.job}-") and made is not None and made.timestamp() < cutoff:
                old.append(f"{name} ({e.get('name', '')})")
        return old

    def _open_endpoint(self, session, state: dict) -> None:
        """A fresh endpoint with the worker on one L4; `state` gets endpoint, url and deployed as each step lands,
        so the caller can close whatever was opened when a step fails."""
        base = self._vertex_base()
        left = self._left_up(session, base)
        if left:
            raise GoogleAPIError("lip-sync: an endpoint of ours is still up from an earlier run and billing: "
                                 + "; ".join(left) + ". Delete it in Vertex AI > Online prediction > Endpoints, "
                                 "then re-run; nothing was started.")
        model = self._model(session, base)
        op = self._call(session, "post", f"{base}/endpoints", "creating an endpoint",
                        {"displayName": f"{self.job}-{int(time.time())}"})
        state["endpoint"] = self._wait_op(session, op, "creating an endpoint", 900)["name"]
        state["url"] = f"https://{self.region}-aiplatform.googleapis.com/v1/{state['endpoint']}"
        deployed_model = {"model": model, "displayName": self.job, "dedicatedResources": {
            "machineSpec": {"machineType": self.machine, "acceleratorType": "NVIDIA_L4", "acceleratorCount": 1},
            "minReplicaCount": 1, "maxReplicaCount": 1}}
        if self.service_account:
            deployed_model["serviceAccount"] = self.service_account
        op = self._call(session, "post", f"{state['url']}:deployModel", "deploying the worker on an L4",
                        {"deployedModel": deployed_model})
        state["deployed"] = self._wait_op(session, op, "deploying the worker on an L4", 3600)["deployedModel"]["id"]

    def _predict_and_wait(self, session, ep_url: str, manifest_uri: str) -> None:
        """Queue one batch on the deployed worker and wait for its status file."""
        for attempt in range(6):                         # the first request right after deploying can be refused
            resp = session.post(f"{ep_url}:predict", json={"instances": [{"manifest": manifest_uri}]})
            if resp.status_code < 300:
                break
            time.sleep(min(60, self.poll_s * (attempt + 1)))
        else:
            raise GoogleAPIError(f"lip-sync: the endpoint refused the batch: {resp.status_code} {resp.text[:500]}")
        b, p = manifest_uri.removeprefix("gs://").split("/", 1)
        done = self.storage().bucket(b).blob(p.removesuffix(".json") + ".done.json")
        deadline = time.time() + self.timeout_s
        while not done.exists():
            if time.time() > deadline:
                raise GoogleAPIError(f"lip-sync: no result after {self.timeout_s / 60:.0f} min; see the endpoint's "
                                     "logs in Vertex AI. Re-run to try again.")
            time.sleep(self.poll_s)

    def _execute_vertex_endpoint(self, manifest_uri: str):
        """Deploy the worker to a fresh endpoint on one L4, queue the batch, wait for its status file, and always
        undeploy and delete the endpoint (an endpoint bills for every minute it is up). Inside _endpoint_kept the
        endpoint is already up and stays up for the next batch; the first batch's time counts from the deploy."""
        if self._shared is not None:
            start = self._shared.pop("start", None) or _now()
            self._predict_and_wait(self._shared["session"], self._shared["url"], manifest_uri)
            return SimpleExecution(start, _now())
        session, state, start = self._session(), {}, _now()
        try:
            self._open_endpoint(session, state)
            self._predict_and_wait(session, state["url"], manifest_uri)
        finally:                                         # stop the billing whatever happened
            if state.get("endpoint"):
                self._tear_down(session, state["endpoint"], state.get("deployed"))
        return SimpleExecution(start, _now())

    @contextlib.contextmanager
    def _endpoint_kept(self):
        """One endpoint for every batch of an apply_many call: deployed once, used by each _run, and always closed
        at the end, whichever batch failed."""
        session, state = self._session(), {}
        try:
            self._open_endpoint(session, state)
            self._shared = {"session": session, "url": state["url"], "start": _now()}
            yield
        finally:
            self._shared = None
            if state.get("endpoint"):
                self._tear_down(session, state["endpoint"], state.get("deployed"))


    def _tear_down(self, session, endpoint: str, deployed: str | None) -> None:
        """Undeploy every model on the endpoint (also one whose deployment failed half-way), then delete it. If that
        cannot be confirmed, say so loudly: a forgotten endpoint keeps billing."""
        ep_url = f"https://{self.region}-aiplatform.googleapis.com/v1/{endpoint}"
        try:
            ids = [d["id"] for d in session.get(ep_url).json().get("deployedModels", [])]
        except Exception:
            ids = []
        ids = ids or ([deployed] if deployed else [])
        problems = []
        for dm in ids:
            try:
                op = self._call(session, "post", f"{ep_url}:undeployModel", "undeploying", {"deployedModelId": dm})
                self._wait_op(session, op, "undeploying", 1800)
            except GoogleAPIError as e:
                problems.append(str(e))
        try:
            self._call(session, "delete", ep_url, "deleting the endpoint")
        except GoogleAPIError as e:
            problems.append(str(e))
        if problems:
            raise GoogleAPIError(f"lip-sync: could not shut down endpoint {endpoint}; delete it in the Vertex AI "
                                 f"console (Online prediction > Endpoints) so it stops billing. " + " | ".join(problems))


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


class SimpleExecution:
    def __init__(self, start_time, completion_time):
        self.start_time, self.completion_time = start_time, completion_time


def _ts(value: str | None):
    if not value:
        return None
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _elapsed(execution) -> float | None:
    start, end = getattr(execution, "start_time", None), getattr(execution, "completion_time", None)
    try:
        return (end - start).total_seconds() if start and end else None
    except TypeError:
        return None
