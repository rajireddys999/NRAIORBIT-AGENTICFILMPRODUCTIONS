"""A tick's fixed cost: restore, backup and the queue sheets go through the storage client (one listing per set, only
what differs by checksum moves, one set at a time with every transfer), gcloud only where no client can be made, and
a tick with nothing to do never touches the library. What lands on disk and in the bucket is what gcloud rsync would
have left, and a transfer that fails never costs a good copy on either side."""

import base64
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import google_crc32c
import pytest
import yaml

from nraiorbit.llm import LLMError
from nraiorbit.ops import backup
from nraiorbit.release import gcs
from nraiorbit.service import queue

pytest.importorskip("google.api_core")


def _crc(data: bytes) -> str:
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode("ascii")


class Store:
    """The bucket's objects (name -> bytes) and every request made of it, from any thread."""

    def __init__(self):
        self.objects, self.calls, self.fail = {}, [], (lambda name: False)

    def of(self, kind):
        return [c[1] for c in self.calls if c[0] == kind]


class Blob:
    def __init__(self, store, name):
        self._s, self.name = store, name
        data = store.objects.get(name)
        self.size, self.crc32c = (None, None) if data is None else (len(data), _crc(data))

    def _data(self):
        from google.api_core.exceptions import NotFound
        if self.name not in self._s.objects:
            raise NotFound(self.name)
        return self._s.objects[self.name]

    def exists(self):
        self._s.calls.append(("exists", self.name))
        return self.name in self._s.objects

    def download_as_text(self, encoding=None, **kw):
        self._s.calls.append(("get", self.name))
        return self._data().decode(encoding or "utf-8")

    def download_to_filename(self, filename, **kw):
        from google.api_core.exceptions import ServiceUnavailable
        self._s.calls.append(("download", self.name))
        if self._s.fail(self.name):
            raise ServiceUnavailable("503 backend error")
        Path(filename).write_bytes(self._data())

    def upload_from_filename(self, filename, content_type=None, **kw):
        self._s.calls.append(("upload", self.name, content_type))
        self._s.objects[self.name] = Path(filename).read_bytes()

    def delete(self, **kw):
        self._s.calls.append(("delete", self.name))
        self._data()
        del self._s.objects[self.name]


class Bucket:
    def __init__(self, store):
        self._s = store

    def blob(self, name):
        return Blob(self._s, name)

    def list_blobs(self, prefix="", max_results=None):
        self._s.calls.append(("list", prefix))
        names = sorted(n for n in self._s.objects if n.startswith(prefix))
        return iter([Blob(self._s, n) for n in (names[:max_results] if max_results else names)])


class Client:
    def __init__(self, store):
        self._s = store

    def bucket(self, name):
        assert name == "bkt"
        return Bucket(self._s)


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    """A storage client over an in-memory bucket; gcloud is recorded, and the library lives under tmp_path."""
    import nraiorbit.config as config
    store = Store()
    store.run = []
    monkeypatch.setattr(gcs, "STORAGE_FACTORY", lambda: Client(store))

    def run(cmd, **kw):
        store.run.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(gcs, "RUN", run)
    monkeypatch.setattr(gcs.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    return store


def _no_client():
    raise RuntimeError("no credentials on this machine")


def _project(store, slug="film"):
    """One object in every folder set, a nested one in inbox, and three of the eight plan files."""
    for folder in backup.FOLDERS.values():
        store.objects[f"{slug}/{folder}/one.bin"] = f"{folder} bytes".encode()
    store.objects[f"{slug}/inbox/video/sc001_s001.mp4"] = b"clip"
    store.objects[f"{slug}/reviews/story.pending"] = b"look\n"
    for f in ("pipeline.yaml", "cast.lock.yaml", "input/story.md"):
        store.objects[f"{slug}/project/{f}"] = f"{f}: 1\n".encode()


# -------------------------------------------------------------------------------------------- the project restore

def test_a_restore_through_the_client_runs_no_gcloud_and_logs_in_set_order(tmp_path, cloud):
    _project(cloud)
    root, lines, timings = tmp_path / "film", [], {}
    done, failed = backup.restore_report(root, "bkt", log=lines.append, timings=timings)
    assert cloud.run == [] and failed == []
    assert done == 10 + 3                                                   # every folder, the three plan files held
    for name, data in cloud.objects.items():
        rel = name.split("/", 1)[1]
        local = root / (rel[len("project/"):] if rel.startswith("project/") else rel)
        assert local.read_bytes() == data, name
    expected = []
    for name, kind, local, remote in backup.named_pairs(root, "bkt"):
        held = kind == "folder" or remote[len("gs://bkt/"):] in cloud.objects
        expected.append(f"  {remote} -> {local.relative_to(root)}" if held else f"  {remote}: nothing there")
    assert lines == expected
    assert list(timings) == backup.DEFAULT_SETS and all(v >= 0 for v in timings.values())


def test_only_a_file_whose_bytes_differ_is_fetched_again_never_by_size(tmp_path, cloud):
    cloud.objects["film/cache/a.json"] = b'{"v": 1}'
    cloud.objects["film/cache/b.json"] = b'{"v": 2}'
    root = tmp_path / "film"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "a.json").write_bytes(b'{"v": 9}')                    # same size, other bytes
    (root / "cache" / "b.json").write_bytes(b'{"v": 2}')                    # identical
    (root / "cache" / "mine.txt").write_bytes(b"local only")               # never removed, as rsync never does
    done, failed = backup.restore_report(root, "bkt", ["cache"], log=lambda *_: None)
    assert (done, failed) == (1, []) and cloud.of("download") == ["film/cache/a.json"]
    assert (root / "cache" / "a.json").read_bytes() == b'{"v": 1}' and (root / "cache" / "mine.txt").exists()


def test_a_name_that_would_leave_the_folder_is_skipped(tmp_path, cloud):
    cloud.objects["film/cache/ok.txt"] = b"ok"
    cloud.objects["film/cache/../escape.txt"] = b"no"
    cloud.objects["film/cache/marker/"] = b""
    backup.restore_report(tmp_path / "film", "bkt", ["cache"], log=lambda *_: None)
    assert cloud.of("download") == ["film/cache/ok.txt"] and not (tmp_path / "film" / "escape.txt").exists()


def _opens_then_fails(cloud, errors: dict):
    """Blob.download_to_filename as google-cloud-storage does it: the file it is given is opened 'wb' before the
    request, so a failure leaves it half-written, and on a NotFound (or a checksum mismatch) the file is deleted."""
    from google.api_core.exceptions import NotFound

    def download_to_filename(self, filename, **kw):
        cloud.calls.append(("download", self.name))
        try:
            with open(filename, "wb") as f:
                f.write(self._data()[:2])
                if self.name in errors:
                    raise errors[self.name]
                f.write(self._data()[2:])
        except NotFound:
            os.remove(filename)
            raise
    return download_to_filename


def test_a_download_that_fails_leaves_the_local_copy_as_it_was(tmp_path, cloud, monkeypatch):
    """A 403, a timeout or a credential refresh halfway, or an object gone since the listing: the local copy (the
    owner's working copy, the image's library file) stays whole, as with gcloud, and no temp file is left behind."""
    from google.api_core.exceptions import Forbidden, NotFound
    cloud.objects.update({"film/cache/a.json": b'{"v": 2}', "film/cache/b.json": b'{"v": 3}',
                          "film/project/pipeline.yaml": b"x: 2\n"})
    root = tmp_path / "film"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "a.json").write_bytes(b'{"v": 1}')
    (root / "pipeline.yaml").write_bytes(b"x: 1\n")
    monkeypatch.setattr(Blob, "download_to_filename", _opens_then_fails(cloud, {
        "film/cache/a.json": Forbidden("403 no access"), "film/project/pipeline.yaml": NotFound("gone")}))
    assert backup.restore_report(root, "bkt", ["cache", "files"], log=lambda *_: None) == (0, ["cache", "files"])
    assert (root / "cache" / "a.json").read_bytes() == b'{"v": 1}' and (root / "pipeline.yaml").read_bytes() == b"x: 1\n"
    assert (root / "cache" / "b.json").read_bytes() == b'{"v": 3}'      # the download that worked landed whole
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
        "cache/a.json", "cache/b.json", "pipeline.yaml"]


def test_sets_move_one_at_a_time_in_set_order_each_with_every_transfer(tmp_path, cloud, monkeypatch):
    """The inbox of paid clips is most of a restore or a backup: it gets all NRAIORBIT_SYNC_WORKERS transfers, not a
    share of them while other sets run beside it."""
    _project(cloud)
    monkeypatch.setenv("NRAIORBIT_SYNC_WORKERS", "5")
    me, calls, real_down, real_up = threading.current_thread(), [], gcs.sync_down, gcs.sync_up

    def down(storage, remote, local, workers=8):
        calls.append(("down", remote.rsplit("/", 1)[1], workers, threading.current_thread() is me))
        return real_down(storage, remote, local, workers)

    def up(storage, local, remote, workers=8, delete=False):
        calls.append(("up", remote.rsplit("/", 1)[1], workers, threading.current_thread() is me))
        return real_up(storage, local, remote, workers, delete=delete)
    monkeypatch.setattr(gcs, "sync_down", down)
    monkeypatch.setattr(gcs, "sync_up", up)
    root = tmp_path / "film"
    assert backup.restore_report(root, "bkt", log=lambda *_: None) == (13, [])
    assert backup.backup(root, "bkt", log=lambda *_: None) == 13
    folders = [backup.FOLDERS[name] for name in backup.DEFAULT_SETS if name != "files"]
    assert calls == [("down", f, 5, True) for f in folders] + [("up", f, 5, True) for f in folders]


def _sheets(tmp_path, state="new"):
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "film", "story_uri": "", "budget_usd": "5", "gates": "ask", "publish": "none",
                          "state": state, "note": "", "updated": ""}], queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    return str(st), str(rf)


def test_a_set_that_fails_to_download_is_failed_and_the_tick_takes_a_strike(tmp_path, cloud):
    _project(cloud)
    cloud.fail = lambda name: "/cache/" in name
    lines = []
    done, failed = backup.restore_report(tmp_path / "film", "bkt", log=lines.append)
    assert failed == ["cache"] and done == 9 + 3
    assert any(line.startswith("    failed (") for line in lines)
    st, rf = _sheets(tmp_path / "q", "approved")
    ran = []
    queue.tick(st, rf, tmp_path / "projects", bucket="bkt", produce_fn=lambda r, b: ran.append(1), learn_fn=None,
               log=lambda *_: None)
    row = queue.load(st, queue.STORY_COLUMNS)[0]
    assert ran == [] and row["state"] == "approved" and row["note"].startswith("restore failed (1): ")
    assert "cache" in row["note"]


# --------------------------------------------------------------------------------------------- the project backup

def test_a_backup_sends_only_what_changed_and_mirrors_reviews_only_when_asked(tmp_path, cloud):
    root = tmp_path / "film"
    for rel, data in {"inbox/a.wav": b"same", "inbox/b.wav": b"new!", "reviews/story.approved": b"ok",
                      "pipeline.yaml": b"x: 1\n", "02_outline.json": b"{}"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    cloud.objects.update({"film/inbox/a.wav": b"same", "film/inbox/b.wav": b"old!", "film/inbox/gone.wav": b"keep",
                          "film/reviews/story.pending": b"spent"})
    timings = {}
    n = backup.backup(root, "bkt", ["inbox", "reviews", "files"], log=lambda *_: None, timings=timings)
    assert n == 2 + 2 and cloud.run == [] and set(timings) == {"inbox", "reviews", "files"}
    uploads = {c[1]: c[2] for c in cloud.calls if c[0] == "upload"}
    assert set(uploads) == {"film/inbox/b.wav", "film/reviews/story.approved", "film/project/pipeline.yaml",
                            "film/project/02_outline.json"}
    assert uploads["film/project/02_outline.json"] == "application/json"
    assert cloud.objects["film/inbox/b.wav"] == b"new!" and "film/reviews/story.pending" in cloud.objects
    assert cloud.of("delete") == []
    cloud.calls.clear()
    backup.backup(root, "bkt", ["inbox", "reviews"], log=lambda *_: None, mirror_reviews=True)
    assert cloud.of("delete") == ["film/reviews/story.pending"] and cloud.of("upload") == []
    assert "film/inbox/gone.wav" in cloud.objects                           # only reviews is mirrored


def test_a_failed_upload_is_raised_after_every_set_settles(tmp_path, cloud, monkeypatch):
    root = tmp_path / "film"
    for rel in ("inbox/a.wav", "reports/costs.jsonl"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")

    def upload(local, bucket, name):
        if "inbox" in name:
            raise OSError("connection reset")
        bucket.blob(name).upload_from_filename(str(local))
    monkeypatch.setattr(gcs, "upload_file", upload)
    with pytest.raises(gcs.BucketError, match="connection reset"):
        backup.backup(root, "bkt", ["inbox", "reports"], log=lambda *_: None)
    assert "film/reports/costs.jsonl" in cloud.objects


# ------------------------------------------------------------------------------------------- no client: gcloud

def test_without_a_client_restore_backup_and_the_sheets_use_gcloud_as_before(tmp_path, cloud, monkeypatch):
    monkeypatch.setattr(gcs, "STORAGE_FACTORY", _no_client)
    root = tmp_path / "film"
    assert backup.restore_report(root, "bkt", ["inbox", "files"], log=lambda *_: None) == (9, [])
    assert cloud.run[0][:4] == ["gcloud", "storage", "rsync", "-r"] and cloud.run[0][-2:] == \
        ["gs://bkt/film/inbox", str(root / "inbox")]
    assert [c[:3] for c in cloud.run[1:]] == [["gcloud", "storage", "cp"]] * 8
    assert cloud.run[1][3:] == ["gs://bkt/film/project/pipeline.yaml", str(root / "pipeline.yaml")]
    cloud.run.clear()
    (root / "pipeline.yaml").write_text("x: 1\n")
    (root / "reviews").mkdir()
    assert backup.backup(root, "bkt", ["inbox", "reviews", "files"], log=lambda *_: None, mirror_reviews=True) == 3
    assert cloud.run == [
        ["gcloud", "storage", "rsync", "-r", str(root / "inbox"), "gs://bkt/film/inbox"],
        ["gcloud", "storage", "rsync", "-r", "--delete-unmatched-destination-objects", str(root / "reviews"),
         "gs://bkt/film/reviews"],
        ["gcloud", "storage", "cp", str(root / "pipeline.yaml"), "gs://bkt/film/project/pipeline.yaml"]]
    cloud.run.clear()

    def cp(cmd, **kw):
        cloud.run.append(cmd)
        Path(cmd[-1]).write_text("slug,state\nfilm,new\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(gcs, "RUN", cp)
    assert queue._fetch("gs://bkt/queue/stories.csv") == "slug,state\nfilm,new\n"
    assert cloud.run[0][:4] == ["gcloud", "storage", "cp", "gs://bkt/queue/stories.csv"]
    assert cloud.calls == []


def test_without_a_client_a_new_files_only_library_backup_asks_gcloud_not_to_clobber(tmp_path, cloud, monkeypatch):
    monkeypatch.setattr(gcs, "STORAGE_FACTORY", _no_client)
    lib = tmp_path / "repo" / "library" / "characters"
    lib.mkdir(parents=True)
    (lib / "x.png").write_bytes(b"x")
    assert backup.sync_library("bkt", "backup", sets=["characters"], log=lambda *_: None, only_missing=True) == 1
    assert cloud.run == [["gcloud", "storage", "rsync", "-r", "--no-clobber", str(lib), "gs://bkt/library/characters"]]


def test_the_sheets_are_read_through_the_client(cloud):
    cloud.objects["queue/stories.csv"] = "slug,state\nfilm,new\n".encode()
    assert queue._fetch("gs://bkt/queue/stories.csv") == "slug,state\nfilm,new\n"
    assert queue._fetch("gs://bkt/queue/references.csv") == ""              # not there: empty, as before
    assert cloud.run == [] and cloud.of("get") == ["queue/stories.csv", "queue/references.csv"]


def test_a_sheet_the_client_cannot_read_is_read_with_gcloud_never_taken_as_empty(tmp_path, cloud, monkeypatch):
    """Stale Application Default Credentials beside a working gcloud login: the client is made and its read fails.
    Read as an empty sheet, add-story would save a one-row sheet (through gcloud) over every other film's row."""
    import google.auth.exceptions

    from nraiorbit import cli
    sheet = queue.csv_of([{"slug": s, "state": "done"} for s in ("rain", "river", "moon")], queue.STORY_COLUMNS)
    cloud.objects["queue/stories.csv"] = sheet.encode()

    def refused(self, **kw):
        raise google.auth.exceptions.RefreshError("Reauthentication is needed")
    monkeypatch.setattr(Blob, "download_as_text", refused)

    def gcloud(cmd, **kw):                                                # gcloud storage cp, either way
        cloud.run.append(cmd)
        src, dst = cmd[-2], cmd[-1]
        if src.startswith("gs://"):
            Path(dst).write_bytes(cloud.objects[src[len("gs://bkt/"):]])
        else:
            cloud.objects[dst[len("gs://bkt/"):]] = Path(src).read_bytes()
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(gcs, "RUN", gcloud)
    assert queue._fetch("gs://bkt/queue/stories.csv") == sheet
    assert cloud.run[0][:4] == ["gcloud", "storage", "cp", "gs://bkt/queue/stories.csv"]
    cli.main(["service", "add-story", "sun", "gs://bkt/stories/sun.md", "--stories", "gs://bkt/queue/stories.csv"])
    rows = queue.rows_of(cloud.objects["queue/stories.csv"].decode(), queue.STORY_COLUMNS)
    assert [r["slug"] for r in rows] == ["rain", "river", "moon", "sun"]


# ------------------------------------------------------------------------------------------------- the library

def test_the_library_restore_fetches_same_size_changes_and_skips_an_empty_set(tmp_path, cloud):
    lib = tmp_path / "repo" / "library" / "characters" / "hero"
    lib.mkdir(parents=True)
    (lib / "manifest.yaml").write_bytes(b"seed: 1\n")
    (lib / "centroid.npy").write_bytes(b"\x93NUMPY" + bytes(16))
    (lib / "face.png").write_bytes(b"png")
    cloud.objects.update({"library/characters/hero/manifest.yaml": b"seed: 2\n",           # same size, edited
                          "library/characters/hero/centroid.npy": b"\x93NUMPY" + bytes(15) + b"\x01",
                          "library/characters/hero/face.png": b"png"})
    lines = []
    assert backup.sync_library("bkt", "restore", log=lines.append) == 1     # references holds nothing: not counted
    assert sorted(cloud.of("download")) == ["library/characters/hero/centroid.npy",
                                            "library/characters/hero/manifest.yaml"]
    assert (lib / "manifest.yaml").read_bytes() == b"seed: 2\n" and cloud.run == []
    assert lines == ["  library/characters <- gs://bkt/library/characters"]
    assert (tmp_path / "repo" / "library" / "references").is_dir()
    (lib / "new.png").write_bytes(b"new")
    cloud.calls.clear()
    assert backup.sync_library("bkt", "backup", log=lambda *_: None) == 1
    assert cloud.of("upload") == ["library/characters/hero/new.png"]


def test_a_library_restore_that_fails_backs_up_only_new_library_files(tmp_path, cloud, monkeypatch):
    """The image ships an older library file than the bucket holds and its download fails: the tick goes on, and its
    library backup sends up only files the bucket lacks (a profile learned in that tick is paid work), so the image's
    copy never replaces the bucket's newer one."""
    from google.api_core.exceptions import Forbidden
    lib = tmp_path / "repo" / "library" / "characters" / "hero"
    lib.mkdir(parents=True)
    (lib / "manifest.yaml").write_bytes(b"seed: 1\n")                                   # the image's copy
    (lib / "new_pose.png").write_bytes(b"made on this disk")                            # the bucket has none
    newer = cloud.objects["library/characters/hero/manifest.yaml"] = b"seed: 2\nimages: [a.png, b.png]\n"
    _project(cloud, "film")
    st, rf = _sheets(tmp_path / "q", "approved")
    monkeypatch.setattr(queue, "TRACKER_FACTORY", lambda b, log=print: Doings())
    monkeypatch.setattr(Blob, "download_to_filename", _opens_then_fails(cloud, {
        "library/characters/hero/manifest.yaml": Forbidden("403 permission blip")}))
    lines = []
    out = queue.tick(st, rf, tmp_path / "projects", bucket="bkt",
                     produce_fn=lambda root, b: {"state": "awaiting:cast", "steps": {}}, learn_fn=None,
                     log=lines.append)
    assert out["story"]["state"] == "awaiting:cast"
    assert (lib / "manifest.yaml").read_bytes() == b"seed: 1\n"
    assert cloud.objects["library/characters/hero/manifest.yaml"] == newer
    assert "  library backup: new files only (the library restore failed)" in lines
    assert [c[1] for c in cloud.calls if c[0] == "upload" and c[1].startswith("library/")] == \
        ["library/characters/hero/new_pose.png"]
    assert cloud.objects["library/characters/hero/new_pose.png"] == b"made on this disk"


def test_only_missing_replaces_nothing_and_a_partial_download_is_never_sent(tmp_path, cloud):
    """sync_up's only_missing is gcloud's --no-clobber; download_blob's temp file, left by a process killed
    mid-download, is never sent up whatever the mode."""
    local = tmp_path / "lib" / "a"
    local.mkdir(parents=True)
    (local / "old.yaml").write_bytes(b"local, older")
    (local / "new.png").write_bytes(b"new")
    (local / ".new.png.k3j9.part").write_bytes(b"half a download")
    cloud.objects["library/x/a/old.yaml"] = b"bucket, newer"
    assert gcs.sync_up(gcs.client(), tmp_path / "lib", "gs://bkt/library/x", 4, only_missing=True) == 1
    assert cloud.objects["library/x/a/old.yaml"] == b"bucket, newer"
    assert cloud.objects["library/x/a/new.png"] == b"new"
    assert not [n for n in cloud.objects if n.endswith(".part")]
    assert gcs.sync_up(gcs.client(), tmp_path / "lib", "gs://bkt/library/x", 4) == 1      # without it: replaced
    assert cloud.objects["library/x/a/old.yaml"] == b"local, older"
    assert not [n for n in cloud.objects if n.endswith(".part")]


# ---------------------------------------------------------------------------------------------------- the tick

class Doings:
    def __init__(self):
        self.doings = []

    def start(self, doing, **fields):
        self.doings.append(doing)

    def close(self):
        pass


def test_an_idle_tick_reads_two_sheets_and_nothing_else(cloud, monkeypatch, tmp_path):
    cloud.objects["queue/stories.csv"] = queue.csv_of([{"slug": "film", "state": "awaiting:story"}],
                                                      queue.STORY_COLUMNS).encode()
    rec = Doings()
    monkeypatch.setattr(queue, "TRACKER_FACTORY", lambda b, log=print: rec)
    out = queue.tick("gs://bkt/queue/stories.csv", "gs://bkt/queue/references.csv", tmp_path / "projects",
                     bucket="bkt", produce_fn=None, learn_fn=None, log=lambda *_: None)
    assert out == {"reference": None, "story": None} and rec.doings == ["idle"]
    assert cloud.run == [] and not [c for c in cloud.calls if "library/" in c[1]]
    assert cloud.of("get") == ["queue/references.csv", "queue/stories.csv"]


def test_a_story_tick_restores_the_library_once_then_the_project_logs_timings_and_mirrors_on_done(
        tmp_path, cloud, monkeypatch):
    _project(cloud, "film")
    cloud.objects["library/characters/hero/face.png"] = b"png"
    st, rf = _sheets(tmp_path / "q", "approved")
    events, seen, real_sync, real_backup = [], {}, backup.sync_library, backup.backup

    def sync_library(bucket, direction, **kw):
        events.append(("library", direction))
        return real_sync(bucket, direction, **kw)

    def spy_backup(root, bucket, **kw):
        seen.update(mirror=kw.get("mirror_reviews"))
        return real_backup(root, bucket, **kw)
    monkeypatch.setattr(backup, "sync_library", sync_library)
    monkeypatch.setattr(backup, "backup", spy_backup)

    def produce_fn(root, bucket):
        events.append(("produce", None))
        assert (tmp_path / "repo" / "library" / "characters" / "hero" / "face.png").exists()
        assert (root / "reviews" / "story.approved").exists()               # restored, then approved on disk
        (root / "reports" / "youtube.json").write_text("{}")
        return {"state": "done", "steps": {"publish": "none"}}
    lines = []
    out = queue.tick(st, rf, tmp_path / "projects", bucket="bkt", produce_fn=produce_fn, learn_fn=None,
                     log=lines.append)
    assert out["story"] == {"slug": "film", "state": "done"} and cloud.run == []
    assert events == [("library", "restore"), ("produce", None), ("library", "backup")]
    assert seen == {"mirror": True} and "film/reports/youtube.json" in cloud.objects
    assert "film/reviews/story.pending" not in cloud.objects                # the consumed marker is mirrored away
    restore = [x for x in lines if x.startswith("  restore: ")]
    backed = [x for x in lines if x.startswith("  backup: ")]
    assert len(restore) == 1 and restore[0].startswith("  restore: 11 sets in ") and "(slowest " in restore[0]
    assert len(backed) == 1 and backed[0].startswith("  backup: ") and "(slowest " in backed[0]
    assert yaml.safe_load((tmp_path / "projects" / "film" / "pipeline.yaml").read_text())["budget"]["max_usd"] == 5.0


def test_a_reference_only_tick_restores_the_library_before_learning_and_backs_it_up_after(
        tmp_path, cloud, monkeypatch):
    """learn() appends to library/references/ledger.jsonl: a tick that skipped the restore would push a one-line
    ledger over the bucket's whole one."""
    ledger = b'{"item": "a"}\n{"item": "b"}\n'
    cloud.objects["library/references/ledger.jsonl"] = ledger
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "film", "state": "awaiting:cast"}], queue.STORY_COLUMNS)    # no story to work on
    queue.save(str(rf), [{"url": "https://youtu.be/abcdefgh", "learn": "all", "state": "new"}], queue.REFERENCE_COLUMNS)
    rec = Doings()
    monkeypatch.setattr(queue, "TRACKER_FACTORY", lambda b, log=print: rec)
    refs = tmp_path / "repo" / "library" / "references"

    def learn_fn(url, title, what, minutes):
        rec.doings.append("learn")
        assert (refs / "ledger.jsonl").read_bytes() == ledger                 # restored before the paid call
        (refs / "ref").mkdir()
        (refs / "ref" / "profile.json").write_text("{}")
        with open(refs / "ledger.jsonl", "a") as f:
            f.write('{"item": "ref"}\n')
        return refs / "ref" / "profile.json"
    out = queue.tick(str(st), str(rf), tmp_path / "projects", bucket="bkt", produce_fn=None, learn_fn=learn_fn,
                     log=lambda *_: None)
    assert out == {"reference": {"url": "https://youtu.be/abcdefgh", "state": "done"}, "story": None}
    assert rec.doings == ["library-restore", "reference", "learn", "library-backup"]
    assert cloud.objects["library/references/ledger.jsonl"] == ledger + b'{"item": "ref"}\n'
    assert cloud.objects["library/references/ref/profile.json"] == b"{}"


def test_a_run_that_fails_after_the_restore_still_backs_up_what_it_saved(tmp_path, cloud, monkeypatch):
    """What the run paid for and saved before it failed (a later scene's shot list that finished while an earlier one
    failed) goes up before the container is gone, under the same reviews rule as a run that ends well; the row keeps
    the run's own error."""
    _project(cloud, "film")
    st, rf = _sheets(tmp_path / "q", "approved")
    seen, real_backup = [], backup.backup

    def spy_backup(root, bucket, **kw):
        seen.append(kw.get("mirror_reviews"))
        return real_backup(root, bucket, **kw)
    monkeypatch.setattr(backup, "backup", spy_backup)

    def produce_fn(root, bucket):
        (root / "04_shots" / "sc003.json").write_text('{"scene_id": "sc003"}')
        raise LLMError("sc002: 429 rate limited")
    lines = []
    out = queue.tick(st, rf, tmp_path / "projects", bucket="bkt", produce_fn=produce_fn, learn_fn=None,
                     log=lines.append)
    row = queue.load(st, queue.STORY_COLUMNS)[0]
    assert out["story"]["state"] == row["state"] == "blocked:LLMError" and row["note"] == "sc002: 429 rate limited"
    assert seen == [True] and cloud.objects["film/04_shots/sc003.json"] == b'{"scene_id": "sc003"}'
    assert "film/reviews/story.approved" in cloud.objects and "film/reviews/story.pending" not in cloud.objects
    assert len([x for x in lines if x.startswith("  backup: ")]) == 1


def test_no_backup_after_a_failed_restore_and_a_failed_backup_never_hides_the_runs_error(tmp_path, cloud, monkeypatch):
    """A project that did not come down whole never goes up: what is on the disk then may be older than the bucket's
    copies. A backup after a failed run that fails too is logged, and the row keeps the run's own error."""
    backed, real_restore = [], backup.restore_report
    monkeypatch.setattr(backup, "backup", lambda root, bucket, **kw: backed.append(root.name))

    def disk_full(root, bucket, **kw):
        raise OSError("disk full")
    for restore, state in ((lambda root, bucket, **kw: (0, ["inbox"]), "approved"),      # a set failed: a strike
                           (disk_full, "blocked:OSError")):
        monkeypatch.setattr(backup, "restore_report", restore)
        st, rf = _sheets(tmp_path / state.replace(":", "_"), "approved")
        queue.tick(st, rf, tmp_path / "projects", bucket="bkt", produce_fn=None, learn_fn=None, log=lambda *_: None)
        assert queue.load(st, queue.STORY_COLUMNS)[0]["state"] == state
    assert backed == []
    _project(cloud, "film")
    monkeypatch.setattr(backup, "restore_report", real_restore)

    def unreachable(root, bucket, **kw):
        raise gcs.BucketError("503 backend error")
    monkeypatch.setattr(backup, "backup", unreachable)
    st, rf = _sheets(tmp_path / "q", "approved")
    lines = []

    def produce_fn(root, bucket):
        raise LLMError("sc002: 429 rate limited")
    queue.tick(st, rf, tmp_path / "projects", bucket="bkt", produce_fn=produce_fn, learn_fn=None, log=lines.append)
    row = queue.load(st, queue.STORY_COLUMNS)[0]
    assert row["state"] == "blocked:LLMError" and row["note"] == "sc002: 429 rate limited"
    assert "  backup after the failure failed: BucketError: 503 backend error" in lines
