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


def pairs(project_root: Path, bucket: str, sets=None) -> list[tuple[str, Path, str]]:
    """(kind, local path, gs:// uri) for every set asked for: 'folder' pairs sync, 'file' pairs copy."""
    project_root = Path(project_root)
    slug = project_root.name
    out = []
    for name in (sets or DEFAULT_SETS):
        if name == "files":
            for f in FILES:
                out.append(("file", project_root / f, f"gs://{bucket}/{slug}/project/{f}"))
        elif name in FOLDERS:
            out.append(("folder", project_root / FOLDERS[name], f"gs://{bucket}/{slug}/{FOLDERS[name]}"))
        else:
            raise ValueError(f"unknown backup set '{name}'; choose from {', '.join([*FOLDERS, 'files'])}")
    return out


def backup(project_root: Path, bucket: str | None = None, sets=None, log=print) -> int:
    bucket = bucket or gcs.default_bucket()
    done = 0
    for kind, local, remote in pairs(project_root, bucket, sets):
        if not local.exists():
            continue
        log(f"  {local.relative_to(Path(project_root))} -> {remote}")
        (gcs.rsync if kind == "folder" else gcs.copy)(local, remote)      # never deletes: a marker the runner
        done += 1                                                          # consumed may come back; it is spent
    return done


def restore(project_root: Path, bucket: str | None = None, sets=None, log=print) -> int:
    bucket = bucket or gcs.default_bucket()
    done = 0
    for kind, local, remote in pairs(project_root, bucket, sets):
        local.parent.mkdir(parents=True, exist_ok=True)
        log(f"  {remote} -> {local.relative_to(Path(project_root))}")
        try:
            if kind == "folder":
                local.mkdir(parents=True, exist_ok=True)
                gcs.rsync(remote, local)
            else:
                gcs.copy(remote, local)
            done += 1
        except gcs.BucketError as e:
            log(f"    skipped ({str(e).splitlines()[-1][:120]})")
    return done


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

