"""The private media bucket: sign-in from the environment, uploads, time-limited download links, folder sync.

Credentials: the service account key's JSON text in GOOGLE_APPLICATION_CREDENTIALS_JSON (what the lip-sync and
Veo providers use), else Application Default Credentials. Signed links need a service account key (a bare access
token cannot sign). Folder sync shells out to `gcloud storage rsync`, which handles large trees and resumes.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
from pathlib import Path

STORAGE_FACTORY = None       # tests inject a fake client here
RUN = subprocess.run         # tests inject a recorder here


class BucketError(RuntimeError):
    pass


def default_bucket(cfg: dict | None = None) -> str:
    ls = (cfg or {}).get("lipsync") or {}
    bucket = ls.get("bucket") or (f"{os.environ['GOOGLE_CLOUD_PROJECT']}-media" if os.environ.get("GOOGLE_CLOUD_PROJECT")
                                  else None)
    if not bucket:
        raise BucketError("no bucket: set lipsync.bucket in pipeline.yaml or GOOGLE_CLOUD_PROJECT in the environment")
    return bucket


def client():
    if STORAGE_FACTORY is not None:
        return STORAGE_FACTORY()
    from google.cloud import storage

    from ..providers.cloud_lipsync import _credentials
    return storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"), credentials=_credentials())


def upload(local: Path, bucket: str, name: str, content_type: str | None = None) -> str:
    blob = client().bucket(bucket).blob(name)
    blob.upload_from_filename(str(local), content_type=content_type)
    return f"gs://{bucket}/{name}"


def signed_url(bucket: str, name: str, days: int = 7, filename: str | None = None) -> str:
    blob = client().bucket(bucket).blob(name)
    kw = {"response_disposition": f'attachment; filename="{filename}"'} if filename else {}
    return blob.generate_signed_url(version="v4", expiration=datetime.timedelta(days=days), method="GET", **kw)


def rsync(src: str | Path, dst: str | Path, delete: bool = False) -> None:
    """Mirror a folder to (or from) the bucket with gcloud. `src`/`dst` are a local path or a gs:// URI."""
    if shutil.which("gcloud") is None:
        raise BucketError("gcloud is not installed; the Google Cloud CLI does the folder sync")
    env = {k: v for k, v in os.environ.items() if k != "CLOUDSDK_AUTH_ACCESS_TOKEN"}   # a preset token hides the SA
    cmd = ["gcloud", "storage", "rsync", "-r", *(["--delete-unmatched-destination-objects"] if delete else []),
           str(src), str(dst)]
    proc = RUN(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise BucketError(f"gcloud storage rsync failed: {proc.stderr[-800:]}")


def copy(src: str | Path, dst: str | Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "CLOUDSDK_AUTH_ACCESS_TOKEN"}
    proc = RUN(["gcloud", "storage", "cp", str(src), str(dst)], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise BucketError(f"gcloud storage cp failed: {proc.stderr[-800:]}")
