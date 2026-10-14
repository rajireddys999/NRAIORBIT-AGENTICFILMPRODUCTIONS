"""Lip sync on your own Google Cloud GPU: LatentSync as a Cloud Run job with one NVIDIA L4 (deploy/lipsync/).

    providers.lipsync: latentsync_cloudrun
    lipsync: {job: nraiorbit-lipsync, region: us-central1, bucket: <project>-media, steps: 20, guidance: 1.5}

All the shots of a render go to the GPU in one job execution (the model loads once): clips and their dialogue audio
are staged in your private bucket under lipsync/<run>/, the job writes the synced clips back with a status file, and
results are cached in cache/lipsync/ by content, so a re-render never pays twice. The GPU time of every execution is
checked against the budget before it starts and logged to reports/cost.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from ..costing import SpendLedger
from .base import register
from .google_ai import GoogleAPIError

STORAGE_FACTORY = None          # tests inject fakes here
JOBS_FACTORY = None


def _credentials():
    """The service account in GOOGLE_APPLICATION_CREDENTIALS_JSON, else None (Application Default Credentials)."""
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
    if not raw:
        return None
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/cloud-platform"])


def gpu_price(pricing: dict) -> dict:
    p = pricing.get("cloud_run_gpu") or {}
    return {"per_hour": p.get("per_hour", 1.10), "startup_minutes": p.get("startup_minutes", 6.0),
            "minutes_per_clip": p.get("minutes_per_clip", 2.0)}


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
        self.price = gpu_price(pricing)
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

    def apply(self, video: Path, audio: Path, out: Path) -> Path:
        return self.apply_many([(video, audio, out)])[0]

    def apply_many(self, items: list[tuple[Path, Path, Path]]) -> list[Path]:
        keys = [self.key(v, a) for v, a, _ in items]
        todo = {k: (v, a) for k, (v, a, _) in zip(keys, items) if not (self.cache / f"{k}.mp4").exists()}
        if todo:
            self._run(todo)
        outs = []
        for k, (_, _, out) in zip(keys, items):
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.cache / f"{k}.mp4", out)
            outs.append(out)
        return outs

    # -- one job execution for every shot not yet synced

    def _run(self, todo: dict[str, tuple[Path, Path]]) -> None:
        p = self.price
        estimate = p["per_hour"] * (p["startup_minutes"] + p["minutes_per_clip"] * len(todo)) / 60
        self.ledger.check(estimate, f"lip-sync of {len(todo)} shots on a Cloud Run L4 GPU")
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
        started = time.time()
        execution = self._execute(f"gs://{self.bucket}/{prefix}/manifest.json")
        seconds = _elapsed(execution) or (time.time() - started)
        self.ledger.record("lipsync", f"{len(todo)} shots", p["per_hour"] * seconds / 3600, seconds=round(seconds),
                           shots=len(todo), job=self.job, run=run)
        done = bucket.blob(f"{prefix}/manifest.done.json")
        if not done.exists():
            raise GoogleAPIError(f"lip-sync job {self.job} wrote no status for run {run}; read its logs in Cloud Run "
                                 f"(region {self.region}). Re-run to try again.")
        status = json.loads(done.download_as_text())
        self.cache.mkdir(parents=True, exist_ok=True)
        failed = []
        for r in status.get("results", []):
            if r.get("ok"):
                tmp = self.cache / f".{r['key']}.part"
                bucket.blob(f"{prefix}/{r['key']}/out.mp4").download_to_filename(str(tmp))
                os.replace(tmp, self.cache / f"{r['key']}.mp4")
            else:
                failed.append(r)
        if failed:
            raise GoogleAPIError(f"lip-sync failed for {len(failed)} of {len(todo)} shots (LatentSync "
                                 f"{status.get('version', '?')}). First error:\n{failed[0].get('error', '')[-1200:]}")

    def _execute(self, manifest_uri: str):
        name = f"projects/{self.project}/locations/{self.region}/jobs/{self.job}"
        try:
            from google.cloud import run_v2

            request = run_v2.RunJobRequest(name=name, overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name="MANIFEST", value=manifest_uri)])]))
        except ImportError:                              # fakes in tests take a plain dict
            request = {"name": name, "manifest": manifest_uri}
        try:
            return self.jobs().run_job(request=request).result(timeout=self.timeout_s)
        except Exception as e:
            if isinstance(e, GoogleAPIError):
                raise
            raise GoogleAPIError(f"lip-sync job {name}: {type(e).__name__}: {e}\nIs the job set up "
                                 "(bash deploy/lipsync/setup.sh)? Re-run to resume.") from None


def _elapsed(execution) -> float | None:
    start, end = getattr(execution, "start_time", None), getattr(execution, "completion_time", None)
    try:
        return (end - start).total_seconds() if start and end else None
    except TypeError:
        return None
