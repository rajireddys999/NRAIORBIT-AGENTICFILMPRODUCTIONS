"""Backup and restore of a project (`nraiorbit backup <slug>`, `nraiorbit restore <slug>`).

Project media is not in git. Everything that was paid for or decided lives in the bucket under <slug>/:
the inbox (clips, hand-made or approved voices, music), the caches (lip-sync results, face analyses, music, TTS),
keyframes, the rendered music, reports (the ledger), the master and the plan files. A restore before a run means
nothing is generated or paid again.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ..release import gcs

FOLDERS = {                    # set name -> folder under the project; synced both ways
    "inbox": "inbox",
    "cache": "cache",
    "keyframes": "06_keyframes",
    "music": "08_music",
    "reports": "reports",
    "master": "11_master",
    "scenes": "03_scenes",
    "shots": "04_shots",
    "post": "post",
    "reviews": "reviews",      # the gates' .pending / .approved marks: state, not media
}
RUN_WRITES = {"reports": ("progress.json", "progress.json.part")}   # the run sends these itself (service/progress),
#                     every two minutes: a backup sending its own copy too would race the heartbeat for the object
# Never 10_edit, 07_video or 05_dialogue: the timeline there points at clips and takes a fresh container re-makes
# from the caches for free, and a restored timeline without them would make the render step skip and the trailer
# fail. Assembly masters again when its intermediates are gone (pipeline._assemble).
FILES = ["pipeline.yaml", "pricing.yaml", "cast.lock.yaml", "takes.yaml", "reuse.yaml", "01_story_bible.json",
         "02_outline.json", "input/story.md"]
DEFAULT_SETS = ["inbox", "cache", "keyframes", "music", "reports", "master", "scenes", "shots", "post", "reviews",
                "files"]
LIBRARY_SETS = ("characters", "references")     # library/<set> <-> gs://bucket/library/<set>, shared by every film


NEVER_REMOVED = ("inbox", "cache", "06_keyframes", "reports", "reviews")   # paid media, history and approvals


def remote_keys(project_root: Path, paths) -> list[tuple[str, bool]]:
    """(bucket key, is a folder) for project paths the backup keeps in the bucket; nothing for the rest (10_edit,
    07_video and 05_dialogue never go up) and never anything under NEVER_REMOVED."""
    root = Path(project_root)
    out = []
    for p in paths:
        try:
            rel = Path(p).relative_to(root).as_posix()
        except ValueError:
            continue
        top = rel.split("/", 1)[0]
        if top in NEVER_REMOVED:
            continue
        if rel in FILES:
            out.append((f"{root.name}/project/{rel}", False))
        elif top in FOLDERS.values():
            out.append((f"{root.name}/{rel}", Path(p).is_dir() or not Path(p).suffix))
    return out


def remove_remote(project_root: Path, bucket: str | None, paths, log=print) -> int:
    """Delete from the bucket the copies of outputs a step being redone threw away locally (Pipeline.invalidate), so
    the next fresh container cannot restore an old trailer or cut and skip making the new one. Objects only, never
    paid media; a failure is logged and the run goes on. Returns the objects removed."""
    keys = remote_keys(project_root, paths)
    if not bucket or not keys:
        return 0
    client, removed = storage_client(), 0
    for key, folder in keys:
        try:
            if client is not None:
                b = client.bucket(bucket)
                blobs = list(b.list_blobs(prefix=key.rstrip("/") + "/")) if folder else [b.blob(key)]
                for blob in blobs:
                    try:
                        blob.delete()
                        removed += 1
                    except Exception as e:                   # NotFound: nothing was there
                        if type(e).__name__ != "NotFound":
                            raise
            else:
                uri = f"gs://{bucket}/{key}" + ("/**" if folder else "")
                proc = gcs.RUN(["gcloud", "storage", "rm", "--quiet", uri], capture_output=True, text=True)
                if proc.returncode == 0:
                    removed += 1
                elif "matched no objects" not in (proc.stderr or "") and "No URLs matched" not in (proc.stderr or ""):
                    raise gcs.BucketError(proc.stderr[-300:])
        except Exception as e:
            log(f"  could not remove the old {key} from the bucket ({type(e).__name__}: {str(e)[:120]}); "
                "a later run may restore it")
    if removed:
        log(f"  removed {removed} old output(s) from the bucket")
    return removed


def put_report(project_root: Path, bucket: str | None, name: str, log=print) -> bool:
    """One file of reports/ up to the bucket now, not at the tick's last backup: what the control room shows while
    the run goes on (the owner's rejection, applied). A failure is logged; the backup sends it later anyway."""
    local = Path(project_root) / FOLDERS["reports"] / name
    client = storage_client() if bucket and local.exists() else None
    if client is None:
        return False
    try:
        gcs.upload_file(local, client.bucket(bucket), f"{Path(project_root).name}/{FOLDERS['reports']}/{name}")
        return True
    except Exception as e:
        log(f"  could not send {name} to the bucket yet ({type(e).__name__}: {str(e)[:120]}); the backup will")
        return False


def sync_workers() -> int:
    """Transfers at once through the one storage client (NRAIORBIT_SYNC_WORKERS, default 8). Sets move one at a time,
    each with all of them: a big set (the inbox of paid clips) is most of a restore, and it gets every transfer."""
    return max(1, int(os.environ.get("NRAIORBIT_SYNC_WORKERS") or 8))


def named_pairs(project_root: Path, bucket: str, sets=None) -> list[tuple[str, str, Path, str]]:
    """(set name, kind, local path, gs:// uri) for every set asked for: 'folder' pairs sync, 'file' pairs copy."""
    project_root = Path(project_root)
    slug = project_root.name
    out = []
    for name in (sets or DEFAULT_SETS):
        if name == "files":
            for f in FILES:
                out.append((name, "file", project_root / f, f"gs://{bucket}/{slug}/project/{f}"))
        elif name in FOLDERS:
            out.append((name, "folder", project_root / FOLDERS[name], f"gs://{bucket}/{slug}/{FOLDERS[name]}"))
        else:
            raise ValueError(f"unknown backup set '{name}'; choose from {', '.join([*FOLDERS, 'files'])}")
    return out


def pairs(project_root: Path, bucket: str, sets=None) -> list[tuple[str, Path, str]]:
    """(kind, local path, gs:// uri) for every set asked for: 'folder' pairs sync, 'file' pairs copy."""
    return [(kind, local, remote) for _, kind, local, remote in named_pairs(project_root, bucket, sets)]


def backup(project_root: Path, bucket: str | None = None, sets=None, log=print, mirror_reviews: bool = False,
           timings: dict | None = None) -> int:
    """Folders only ever gain objects in the bucket, so a marker the runner consumed may come back on the next
    restore (it is spent, and nothing acts on it). With `mirror_reviews` the reviews folder is mirrored with deletes
    instead: only a caller that restored that folder in this very run may ask for it, or a failed restore would
    wipe every approval the bucket holds. With a storage client only what changed goes up, one set at a time with
    sync_workers() transfers each, and a set that fails does not keep the later sets from going up: the first failure
    is raised at the end. Without one, gcloud does it one set at a time. `timings` gets {set name: seconds}."""
    bucket = bucket or gcs.default_bucket()
    client = storage_client()
    if client is None:
        return _backup_gcloud(project_root, bucket, sets, log, mirror_reviews, timings)
    workers, done, first = sync_workers(), 0, None
    for name, kind, local, remote in named_pairs(project_root, bucket, sets):
        if not local.exists():
            continue
        t0 = time.monotonic()
        log(f"  {local.relative_to(Path(project_root))} -> {remote}")
        try:
            if kind == "folder":
                gcs.sync_up(client, local, remote, workers, delete=mirror_reviews and name == "reviews",
                            exclude=RUN_WRITES.get(name, ()))
            else:
                b, key = remote[5:].split("/", 1)
                gcs.upload_file(local, client.bucket(b), key)
            done += 1
        except Exception as e:
            if first is None:
                first = e
        if timings is not None:
            timings[name] = timings.get(name, 0.0) + time.monotonic() - t0
    if first is not None:                           # the first failure in set order
        raise first if isinstance(first, gcs.BucketError) else gcs.BucketError(
            f"{type(first).__name__}: {first}"[-800:]) from first
    return done


def _backup_gcloud(project_root, bucket, sets, log, mirror_reviews, timings) -> int:
    done = 0
    for name, kind, local, remote in named_pairs(project_root, bucket, sets):
        if not local.exists():
            continue
        t0 = time.monotonic()
        log(f"  {local.relative_to(Path(project_root))} -> {remote}")
        if kind == "folder":
            gcs.rsync(local, remote, **({"delete": True} if mirror_reviews and name == "reviews" else {}),
                      **({"exclude": RUN_WRITES[name]} if name in RUN_WRITES else {}))
        else:
            gcs.copy(local, remote)
        done += 1
        if timings is not None:
            timings[name] = timings.get(name, 0.0) + time.monotonic() - t0
    return done


NO_MATCH = ("did not find existing container at",     # gcloud storage rsync: a prefix with nothing under it
            "matched no objects or files")             # gcloud storage cp: a file that is not there


def storage_client():
    """The storage client, or None where none can be made (no credentials on this machine)."""
    try:
        return gcs.client()
    except Exception:
        return None


def present(remote: str, kind: str, client=None) -> bool | None:
    """Whether the bucket holds the set at all, asked with the storage client: the file exists, or one object sits
    under the folder's prefix. None when the client cannot answer here (no credentials on this machine); the caller
    then falls back to gcloud's own no-match messages, and nothing else counts as 'nothing there'."""
    client = client or storage_client()
    if client is None:
        return None
    try:
        bucket_name, _, key = remote[5:].partition("/")
        bucket = client.bucket(bucket_name)
        if kind == "file":
            return bool(bucket.blob(key).exists())
        return next(iter(bucket.list_blobs(prefix=key.rstrip("/") + "/", max_results=1)), None) is not None
    except Exception:
        return None


def restore_report(project_root: Path, bucket: str | None = None, sets=None, log=print,
                   timings: dict | None = None) -> tuple[int, list[str]]:
    """Restore every set; returns (sets restored, names of sets that FAILED). A set the bucket does not hold is
    skipped, not failed: a project's first run, a plan file this project never had. The runner refuses to produce
    on a half-restored project: it would pay again for clips it already has, or pass gates nobody looked at. With a
    storage client, sets come down one at a time with sync_workers() transfers each, and only files that differ by
    checksum are fetched. `timings` gets {set name: seconds}."""
    bucket = bucket or gcs.default_bucket()
    client = storage_client()                                 # one client for the whole restore
    if client is None:
        return _restore_gcloud(project_root, bucket, sets, log, timings)
    workers, done, failed = sync_workers(), 0, []
    for name, kind, local, remote in named_pairs(project_root, bucket, sets):
        t0 = time.monotonic()
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            if present(remote, kind, client) is False:
                log(f"  {remote}: nothing there")
                continue
            log(f"  {remote} -> {local.relative_to(Path(project_root))}")
            if kind == "folder":                              # with a client, a NotFound here is a failure: present()
                local.mkdir(parents=True, exist_ok=True)      # did not say the set was absent
                gcs.sync_down(client, remote, local, workers)
            else:
                b, key = remote[5:].split("/", 1)
                gcs.download_blob(client.bucket(b).blob(key), local)
            done += 1
        except Exception as e:
            log(f"    failed ({(str(e).splitlines() or [type(e).__name__])[-1][:120]})")
            if name not in failed:
                failed.append(name)
        finally:
            if timings is not None:
                timings[name] = timings.get(name, 0.0) + time.monotonic() - t0
    return done, failed


def _restore_gcloud(project_root, bucket, sets, log, timings) -> tuple[int, list[str]]:
    done, failed = 0, []
    for name, kind, local, remote in named_pairs(project_root, bucket, sets):
        t0 = time.monotonic()
        local.parent.mkdir(parents=True, exist_ok=True)
        there = present(remote, kind, None)
        try:
            if there is False:
                log(f"  {remote}: nothing there")
                continue
            log(f"  {remote} -> {local.relative_to(Path(project_root))}")
            try:
                if kind == "folder":
                    local.mkdir(parents=True, exist_ok=True)
                    gcs.rsync(remote, local)
                else:
                    gcs.copy(remote, local)
                done += 1
            except gcs.BucketError as e:
                if there is None and any(s in str(e).lower() for s in NO_MATCH):   # no client to ask: gcloud's words
                    log("    nothing there")
                    continue
                log(f"    failed ({str(e).splitlines()[-1][:120]})")
                if name not in failed:
                    failed.append(name)
        finally:
            if timings is not None:
                timings[name] = timings.get(name, 0.0) + time.monotonic() - t0
    return done, failed


def restore(project_root: Path, bucket: str | None = None, sets=None, log=print) -> int:
    return restore_report(project_root, bucket, sets, log)[0]


def sync_library(bucket: str, direction: str = "backup", sets=LIBRARY_SETS, log=print, only_missing: bool = False) -> int:
    """The shared library (identity packs under characters/, craft notes under references/) to or from the bucket.
    A runner with an empty disk restores it before a tick so it never pays again for images it already has. With a
    storage client only files that differ by checksum move (the image already holds most of the library). A backup
    with `only_missing` sends up only files the bucket lacks and replaces nothing: after a failed restore the disk
    holds the image's older copies, but a profile learned in that tick is new and paid for."""
    from ..config import REPO_ROOT
    from ..release import gcs

    client = storage_client()
    n = 0
    for name in sets:
        local, remote = REPO_ROOT / "library" / name, f"gs://{bucket}/library/{name}"
        if direction == "backup":
            if not local.exists() or not any(local.rglob("*")):
                continue
            if client is not None:
                gcs.sync_up(client, local, remote, sync_workers(), only_missing=only_missing)
            else:
                gcs.rsync(local, remote, no_clobber=only_missing)
        else:
            local.mkdir(parents=True, exist_ok=True)
            if client is not None:
                if present(remote, "folder", client) is False:   # nothing there yet is not an error
                    continue
                gcs.sync_down(client, remote, local, sync_workers())
            else:
                try:
                    gcs.rsync(remote, local)
                except gcs.BucketError as exc:            # nothing there yet is not an error
                    text = str(exc).lower()
                    if not any(w in text for w in ("not found", "did not find", "no url", "no such")):
                        raise
                    continue
        n += 1
        log(f"  library/{name} {'->' if direction == 'backup' else '<-'} {remote}")
    return n
