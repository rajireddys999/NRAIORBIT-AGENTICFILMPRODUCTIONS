"""Backup and restore of a project (`nraiorbit backup <slug>`, `nraiorbit restore <slug>`).

Project media is not in git. Everything that was paid for or decided lives in the bucket under <slug>/:
the inbox (clips, hand-made or approved voices, music), the caches (lip-sync results, face analyses, music, TTS),
keyframes, the rendered music, reports (the ledger), the master and the plan files. A restore before a run means
nothing is generated or paid again.
"""

from __future__ import annotations

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
FILES = ["pipeline.yaml", "pricing.yaml", "cast.lock.yaml", "takes.yaml", "reuse.yaml", "01_story_bible.json",
         "02_outline.json", "input/story.md"]
DEFAULT_SETS = ["inbox", "cache", "keyframes", "music", "reports", "master", "scenes", "shots", "post", "reviews",
                "files"]
LIBRARY_SETS = ("characters", "references")     # library/<set> <-> gs://bucket/library/<set>, shared by every film


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


def backup(project_root: Path, bucket: str | None = None, sets=None, log=print, mirror_reviews: bool = False) -> int:
    """Folders only ever gain objects in the bucket, so a marker the runner consumed may come back on the next
    restore (it is spent, and nothing acts on it). With `mirror_reviews` the reviews folder is mirrored with deletes
    instead: only a caller that restored that folder in this very run may ask for it, or a failed restore would
    wipe every approval the bucket holds."""
    bucket = bucket or gcs.default_bucket()
    done = 0
    for name, kind, local, remote in named_pairs(project_root, bucket, sets):
        if not local.exists():
            continue
        log(f"  {local.relative_to(Path(project_root))} -> {remote}")
        if kind == "folder":
            gcs.rsync(local, remote, **({"delete": True} if mirror_reviews and name == "reviews" else {}))
        else:
            gcs.copy(local, remote)
        done += 1
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


def restore_report(project_root: Path, bucket: str | None = None, sets=None, log=print) -> tuple[int, list[str]]:
    """Restore every set; returns (sets restored, names of sets that FAILED). A set the bucket does not hold is
    skipped, not failed: a project's first run, a plan file this project never had. The runner refuses to produce
    on a half-restored project: it would pay again for clips it already has, or pass gates nobody looked at."""
    bucket = bucket or gcs.default_bucket()
    done, failed = 0, []
    client = storage_client()                                 # one client for the whole restore
    for name, kind, local, remote in named_pairs(project_root, bucket, sets):
        local.parent.mkdir(parents=True, exist_ok=True)
        there = present(remote, kind, client)
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
    return done, failed


def restore(project_root: Path, bucket: str | None = None, sets=None, log=print) -> int:
    return restore_report(project_root, bucket, sets, log)[0]


def sync_library(bucket: str, direction: str = "backup", sets=LIBRARY_SETS, log=print) -> int:
    """The shared library (identity packs under characters/, craft notes under references/) to or from the bucket.
    A runner with an empty disk restores it before a tick so it never pays again for images it already has."""
    from ..config import REPO_ROOT
    from ..release import gcs

    n = 0
    for name in sets:
        local, remote = REPO_ROOT / "library" / name, f"gs://{bucket}/library/{name}"
        if direction == "backup":
            if not local.exists() or not any(local.rglob("*")):
                continue
            gcs.rsync(local, remote)
        else:
            local.mkdir(parents=True, exist_ok=True)
            try:
                gcs.rsync(remote, local)
            except gcs.BucketError as exc:                # nothing there yet is not an error
                text = str(exc).lower()
                if not any(w in text for w in ("not found", "did not find", "no url", "no such")):
                    raise
                continue
        n += 1
        log(f"  library/{name} {'->' if direction == 'backup' else '<-'} {remote}")
    return n

