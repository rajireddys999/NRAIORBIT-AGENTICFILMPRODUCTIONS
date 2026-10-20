"""The private media bucket: sign-in from the environment, uploads, time-limited download links, folder sync.

Credentials: the service account key's JSON text in GOOGLE_APPLICATION_CREDENTIALS_JSON (what the lip-sync and
Veo providers use), else Application Default Credentials. Signed links need a service account key (a bare access
token cannot sign). Folder sync goes through the storage client where one can be made (sync_down / sync_up: one
listing, then only what differs by checksum, in parallel), and shells out to `gcloud storage rsync` where it cannot.
Each gcloud call costs seconds of start-up, and a tick makes about twenty of them.
"""

from __future__ import annotations

import base64
import datetime
import mimetypes
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
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


def rsync(src: str | Path, dst: str | Path, delete: bool = False, no_clobber: bool = False) -> None:
    """Mirror a folder to (or from) the bucket with gcloud. `src`/`dst` are a local path or a gs:// URI. With
    `no_clobber`, only what the destination lacks is copied; nothing there is replaced."""
    if shutil.which("gcloud") is None:
        raise BucketError("gcloud is not installed; the Google Cloud CLI does the folder sync")
    env = {k: v for k, v in os.environ.items() if k != "CLOUDSDK_AUTH_ACCESS_TOKEN"}   # a preset token hides the SA
    cmd = ["gcloud", "storage", "rsync", "-r", *(["--delete-unmatched-destination-objects"] if delete else []),
           *(["--no-clobber"] if no_clobber else []), str(src), str(dst)]
    proc = RUN(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise BucketError(f"gcloud storage rsync failed: {proc.stderr[-800:]}")


def copy(src: str | Path, dst: str | Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "CLOUDSDK_AUTH_ACCESS_TOKEN"}
    proc = RUN(["gcloud", "storage", "cp", str(src), str(dst)], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise BucketError(f"gcloud storage cp failed: {proc.stderr[-800:]}")


# ------------------------------------------------------------------------- folder sync through the storage client

def crc32c_b64(path: str | Path) -> str:
    """The file's CRC32C in the form GCS reports it (blob.crc32c: base64 of the 4-byte big-endian digest)."""
    import google_crc32c

    c = google_crc32c.Checksum()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            c.update(chunk)
    return base64.b64encode(c.digest()).decode("ascii")


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = uri[5:].partition("/")
    return bucket, key.rstrip("/")


def read_text(uri: str, storage=None) -> str | None:
    """A gs:// object's text, or None when there is no such object. `storage` is a client already made."""
    from google.api_core.exceptions import NotFound

    bucket, key = _split(uri)
    try:
        return (storage or client()).bucket(bucket).blob(key).download_as_text(encoding="utf-8")
    except NotFound:
        return None


def download_blob(blob, local: str | Path) -> None:
    """Into a temp file beside the target, moved over it only once the download succeeded. The client opens the
    file it is given 'wb' before the request, so a download straight onto the target that fails (403, 429, a
    timeout, a credential refresh) would leave the good local copy empty, and a NotFound or a checksum mismatch
    would delete it. The gcloud path never touched it either: it wrote a temp file and renamed it."""
    local = Path(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=local.parent, prefix=f".{local.name}.", suffix=".part")
    os.close(fd)
    try:
        blob.download_to_filename(tmp)
        os.replace(tmp, local)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _partial(path: Path) -> bool:
    """download_blob's own temp file, left behind only when a process is killed mid-download: never sent up."""
    return path.name.startswith(".") and path.name.endswith(".part")


def upload_file(local: str | Path, bucket, name: str) -> None:
    """`bucket` is a client's bucket object. The content type is guessed from the name, as gcloud does."""
    bucket.blob(name).upload_from_filename(str(local), content_type=mimetypes.guess_type(name)[0])


def _safe(rel: str) -> bool:
    """A name under the prefix that maps to a file inside the local folder: never a folder marker, an absolute
    path or a step out of the folder."""
    return bool(rel) and not rel.endswith("/") and not rel.startswith("/") and ".." not in rel.split("/")


def _listing(storage, uri: str) -> tuple[object, str, dict]:
    """(bucket, key, {relative name: blob}) for every object under the prefix."""
    bucket_name, key = _split(uri)
    bucket = storage.bucket(bucket_name)
    prefix = key + "/"
    out = {}
    for blob in bucket.list_blobs(prefix=prefix):
        rel = blob.name[len(prefix):]
        if _safe(rel):
            out[rel] = blob
    return bucket, key, out


def _differs(local: Path, blob) -> bool:
    """Never by size alone: a manifest edit or a fixed-shape .npy keeps its size and changes its bytes."""
    return not local.is_file() or not blob.crc32c or crc32c_b64(local) != blob.crc32c


def _run_all(jobs: list, workers: int) -> int:
    """Run the jobs (each returns 1 when it moved something) on threads sharing the one client; every job settles
    before the first error, in job order, is raised."""
    if not jobs:
        return 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
        futures = [pool.submit(job) for job in jobs]
    errors = [f.exception() for f in futures if f.exception() is not None]
    if errors:
        e = errors[0]
        raise BucketError(f"{type(e).__name__}: {e}"[-800:]) from e
    return sum(f.result() for f in futures)


def sync_down(storage, remote_prefix_uri: str, local_dir: str | Path, workers: int = 8) -> int:
    """Bring down every object under the prefix whose local copy is missing or differs; returns how many came.
    Like `gcloud storage rsync` without deletes, it never removes a local file, and a download that fails leaves the
    local copy as it was (download_blob)."""
    local_dir = Path(local_dir)
    try:
        _, _, remote = _listing(storage, remote_prefix_uri)
    except Exception as e:
        raise BucketError(f"listing {remote_prefix_uri} failed: {type(e).__name__}: {e}"[-800:]) from e

    def job(rel, blob):
        def run():
            target = local_dir / rel
            if not _differs(target, blob):
                return 0
            download_blob(blob, target)
            return 1
        return run
    return _run_all([job(rel, blob) for rel, blob in sorted(remote.items())], workers)


def sync_up(storage, local_dir: str | Path, remote_prefix_uri: str, workers: int = 8, delete: bool = False,
            only_missing: bool = False) -> int:
    """Send up every local file that is missing under the prefix or differs; with `delete`, also remove the objects
    that have no local file (gcloud's --delete-unmatched-destination-objects). With `only_missing`, an object that
    exists is never replaced, whatever it holds (gcloud's --no-clobber). Returns how many objects changed."""
    local_dir = Path(local_dir)
    try:
        bucket, key, remote = _listing(storage, remote_prefix_uri)
    except Exception as e:
        raise BucketError(f"listing {remote_prefix_uri} failed: {type(e).__name__}: {e}"[-800:]) from e
    local = {p.relative_to(local_dir).as_posix(): p for p in sorted(local_dir.rglob("*"))
             if p.is_file() and not _partial(p)}

    def put(rel, path):
        def run():
            blob = remote.get(rel)
            if blob is not None and (only_missing or not _differs(path, blob)):
                return 0
            upload_file(path, bucket, f"{key}/{rel}")
            return 1
        return run

    def drop(blob):
        def run():
            blob.delete()
            return 1
        return run
    jobs = [put(rel, path) for rel, path in local.items()]
    if delete:
        jobs += [drop(blob) for rel, blob in sorted(remote.items()) if rel not in local]
    return _run_all(jobs, workers)
