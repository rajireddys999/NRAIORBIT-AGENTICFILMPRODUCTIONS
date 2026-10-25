"""The review gates as files: the step gates fire only when a project lists them, and the runner's own gates
(cost, faces, publish) leave the same pending marker a pipeline gate does, so an approval from the control room
or the queue sheet reaches the next run."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nraiorbit.pipeline import STAGE_GATES, STEP_GATES, GatePending, Pipeline, ProjectPaths
from nraiorbit.service import produce


def _p(tmp_path, review_gates):
    return SimpleNamespace(cfg={"review_gates": review_gates}, p=ProjectPaths(tmp_path))


def test_step_gates_are_opt_in_and_the_classic_gates_are_not(tmp_path):
    for name in STEP_GATES:
        Pipeline.gate(_p(tmp_path, {}), name)                                  # unlisted: no stop, no marker
    assert not (tmp_path / "reviews").exists()
    with pytest.raises(GatePending):
        Pipeline.gate(_p(tmp_path, {"outline": {"auto_approve": False}}), "outline")
    assert (tmp_path / "reviews" / "outline.pending").exists()
    Pipeline.gate(_p(tmp_path, {"outline": {"auto_approve": True}}), "outline")  # listed but waved through
    with pytest.raises(GatePending):
        Pipeline.gate(_p(tmp_path, {}), "story")                                 # the classic gates stop as before
    (tmp_path / "reviews" / "story.approved").write_text("ok")
    Pipeline.gate(_p(tmp_path, {}), "story")                                     # approved: through
    assert STEP_GATES | {"story", "cast", "pacing", "trailer"} <= set(STAGE_GATES.values())


def test_runner_gates_leave_the_same_marker_and_read_the_approval(tmp_path):
    p = _p(tmp_path, {})
    produce.hold(p, "cost", '{"total_usd": 1.0}\n')
    assert (tmp_path / "reviews" / "cost.pending").read_text().startswith("{") and not produce.approved(p, "cost")
    produce.approve(p, "cost")
    assert produce.approved(p, "cost") and not (tmp_path / "reviews" / "cost.pending").exists()
    assert produce.GATE_ORDER[:6] == ["story", "outline", "scenes", "cast", "shots", "cost"]
    assert produce.GATE_ORDER[-1] == "publish" and set(produce.RUNNER_GATES) == {"cost", "faces", "publish"}


def test_the_queue_approves_the_gate_the_run_stopped_at_never_a_spent_marker(tmp_path):
    from nraiorbit.service import queue
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    for n in ("story.pending", "story.approved", "outline.pending", "outline.approved", "scenes.pending"):
        (reviews / n).write_text("x")
    assert queue.pending_gate(tmp_path) == "scenes"                      # the only marker never approved
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "produce.json").write_text('{"state": "awaiting:outline"}')
    assert queue.pending_gate(tmp_path) == "outline"                     # the run's own state wins (a re-armed gate)
    (tmp_path / "reports" / "produce.json").write_text('{"state": "awaiting:nothing"}')
    assert queue.pending_gate(tmp_path) == "scenes"
    queue.approve_gate(tmp_path, "scenes")
    assert (reviews / "scenes.approved").read_text() == "approved in the queue\nx" and not (reviews / "scenes.pending").exists()


def test_a_bucket_that_keeps_spent_markers_does_not_deadlock_the_queue(tmp_path, monkeypatch):
    """Cloud Run starts every tick on an empty disk and restores the project from the bucket, and a mirror can bring
    back a marker the runner already consumed. The row is claimed before the restore, the approval goes to the gate
    the run stopped at and never to a spent marker, and a film that stops at every step still reaches done."""
    import json
    import shutil

    from nraiorbit.ops import backup
    from nraiorbit.service import queue
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "rain", "story_uri": str(tmp_path / "rain.md"), "budget_usd": "40", "gates": "ask",
                          "publish": "none", "state": "new", "note": "", "updated": ""}], queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    projects, bucket = tmp_path / "projects", tmp_path / "bucket"
    template = projects / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    def restore_report(root, b, log=None, **k):                # bucket -> empty disk, nothing ever missing or failing
        if (bucket / root.name).exists():
            shutil.copytree(bucket / root.name, root, dirs_exist_ok=True)
        return 1, []
    monkeypatch.setattr(backup, "restore_report", restore_report)
    monkeypatch.setattr(backup, "backup", lambda root, b, log=None, mirror_reviews=False, **k:
                        shutil.copytree(root, bucket / root.name, dirs_exist_ok=True))    # a union: nothing deleted
    monkeypatch.setattr(backup, "sync_library", lambda *a, **k: None)
    order = ["story", "outline", "scenes", "cast"]

    def produce_fn(root, b):                     # stops at each gate in turn, as produce does, and writes its report
        assert queue.load(str(st), queue.STORY_COLUMNS)[0]["state"] == "producing"   # claimed before any work
        if not (bucket / "rain").exists():       # during the first run the owner queues another film
            rows_now = queue.load(str(st), queue.STORY_COLUMNS)
            rows_now.append({"slug": "other", "story_uri": "", "budget_usd": "1", "gates": "ask", "publish": "none",
                             "state": "blocked:owner", "note": "", "updated": ""})
            queue.save(str(st), rows_now, queue.STORY_COLUMNS)
        reviews = root / "reviews"
        reviews.mkdir(exist_ok=True)
        state = "done"
        for g in order:
            if not (reviews / f"{g}.approved").exists():
                (reviews / f"{g}.pending").write_text(f"look at {g}\n")
                state = f"awaiting:{g}"
                break
        (root / "reports").mkdir(exist_ok=True)
        (root / "reports" / "produce.json").write_text(json.dumps({"state": state, "steps": {}}))
        return {"state": state, "steps": {}}

    seen = []
    for _ in range(12):
        shutil.rmtree(projects / "rain", ignore_errors=True)              # a fresh container's empty disk
        queue.tick(str(st), str(rf), projects, bucket="b", produce_fn=produce_fn, learn_fn=None, template=template,
                   log=lambda *_: None)
        rows = queue.load(str(st), queue.STORY_COLUMNS)
        seen.append(rows[0]["state"])
        if rows[0]["state"] == "done":
            break
        rows[0]["state"] = "approved"
        queue.save(str(st), rows, queue.STORY_COLUMNS)
    assert seen == ["awaiting:story", "awaiting:outline", "awaiting:scenes", "awaiting:cast", "done"]
    marks = {p.name for p in (bucket / "rain" / "reviews").iterdir()}
    assert "story.pending" in marks and "cast.approved" in marks         # the stale marker stayed and did no harm
    assert (bucket / "rain" / "reviews" / "cast.approved").read_text().endswith("look at cast\n")
    others = [r for r in queue.load(str(st), queue.STORY_COLUMNS) if r["slug"] == "other"]
    assert others and others[0]["state"] == "blocked:owner"            # a row added mid-run survived every save


def test_a_producing_row_is_another_ticks_while_its_claim_is_fresh(monkeypatch):
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    fresh = {"slug": "a", "state": "producing", "updated": queue.now()}
    stale = {"slug": "b", "state": "producing", "updated": "2026-01-01T00:00:00+00:00"}
    new = {"slug": "c", "state": "new", "updated": ""}
    assert queue.ready_story([fresh]) is None and queue.ready_story([stale]) is stale
    assert queue.ready_story([fresh, new]) is new
    monkeypatch.setenv("NRAIORBIT_CLAIM_HOURS", "0")
    assert queue.ready_story([fresh]) is fresh


def test_invalidating_a_stage_clears_the_runner_gates_it_makes_stale(tmp_path):
    p = _p(tmp_path, {})
    p.p.reviews.mkdir()
    for n in ("cost.approved", "faces.approved", "publish.approved", "story.approved"):
        (p.p.reviews / n).write_text("x")
    Pipeline.invalidate(p, "trailer")
    assert not (p.p.reviews / "publish.approved").exists() and (p.p.reviews / "cost.approved").exists()
    Pipeline.invalidate(p, "render")
    assert not (p.p.reviews / "cost.approved").exists() and not (p.p.reviews / "faces.approved").exists()
    assert (p.p.reviews / "story.approved").exists()


def test_an_upload_record_speaks_only_for_the_very_file_it_recorded(tmp_path):
    movie = tmp_path / "movie.mp4"
    movie.write_bytes(b"cut one")
    record = {"film": {"id": "vid1", "file": produce.fingerprint(movie)}}
    assert produce.uploaded_already(record, "film", movie)
    movie.write_bytes(b"cut two, re-assembled off the runner")       # an old record restored over a new cut
    assert not produce.uploaded_already(record, "film", movie)
    assert not produce.uploaded_already({"film": {"id": "vid1"}}, "film", movie)   # a record with no fingerprint


def test_publishing_uploads_each_cut_once_and_records_it(tmp_path, monkeypatch):
    """The real publish path with YouTube's own return type (a video id string): the film and the trailer go up
    once each and are recorded; a second run uploads nothing; a re-cut trailer goes up again, the film does not."""
    from nraiorbit.demo import OUTLINE, STORY_BIBLE
    from nraiorbit.publish import youtube
    from nraiorbit.schemas import Outline, StoryBible
    root = tmp_path / "film"
    (root / "11_master").mkdir(parents=True)
    (root / "01_story_bible.json").write_text(StoryBible.model_validate(STORY_BIBLE).model_dump_json(), encoding="utf-8")
    (root / "02_outline.json").write_text(Outline.model_validate(OUTLINE).model_dump_json(), encoding="utf-8")
    (root / "11_master" / "movie.mp4").write_bytes(b"the film")
    (root / "11_master" / "trailer.mp4").write_bytes(b"the trailer")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS", str(tmp_path / "missing.json"))
    monkeypatch.setattr(youtube, "token_available", lambda token: True)
    monkeypatch.setattr(youtube, "credentials", lambda secrets, token, interactive=True: "creds")
    monkeypatch.setattr(youtube, "build_metadata", lambda bible, outline, privacy: {"snippet": {"title": "t"}})
    sent, kept = [], []
    monkeypatch.setattr(youtube, "upload", lambda video, meta, creds, captions=None, thumbnail=None:
                        sent.append(video.name) or f"id-{len(sent)}")
    from nraiorbit.ops import backup                   # each upload is in the bucket's record before the next one
    monkeypatch.setattr(backup, "put_report", lambda root_, bucket, name, log=print: kept.append(
        (bucket, name, sorted(json.loads((root_ / "reports" / name).read_text())), list(sent))) or True)
    out = produce.publish_youtube(root, "private", bucket="bkt")
    assert sent == ["movie.mp4", "trailer.mp4"] and out["film"]["id"] == "id-1" and out["trailer"]["id"] == "id-2"
    assert kept == [("bkt", "youtube.json", ["film"], ["movie.mp4"]),
                    ("bkt", "youtube.json", ["film", "trailer"], ["movie.mp4", "trailer.mp4"])]
    assert (root / "reports" / "youtube.json").exists()
    assert produce.publish_youtube(root, "private") == out and sent == ["movie.mp4", "trailer.mp4"]
    (root / "11_master" / "trailer.mp4").write_bytes(b"the trailer, re-cut")
    out = produce.publish_youtube(root, "private")
    assert sent == ["movie.mp4", "trailer.mp4", "trailer.mp4"] and out["trailer"]["id"] == "id-3"


def test_the_cost_approval_remembers_the_figure(tmp_path):
    p = _p(tmp_path, {})
    produce.hold(p, "cost", '{"total_usd": 12.5}\n')
    produce.approve(p, "cost", "approved in the queue\n" + (tmp_path / "reviews" / "cost.pending").read_text())
    assert produce.approved_figure(p, "cost") == 12.5
    produce.approve(p, "cost")
    assert produce.approved_figure(p, "cost") is None                     # an old approval carries no figure


def test_one_films_row_is_written_without_touching_the_others(tmp_path):
    from nraiorbit.service import queue
    st = tmp_path / "stories.csv"
    base = {"story_uri": "", "budget_usd": "1", "gates": "ask", "publish": "none", "note": "", "updated": ""}
    queue.save(str(st), [{**base, "slug": "a", "state": "new"}, {**base, "slug": "b", "state": "new"}], queue.STORY_COLUMNS)
    stale = queue.load(str(st), queue.STORY_COLUMNS)                     # what a long tick loaded hours ago
    queue.write_story(str(st), {**base, "slug": "b", "state": "awaiting:story"})     # the owner's film moved on
    edited = queue.load(str(st), queue.STORY_COLUMNS)                    # meanwhile the owner lowered a's cap
    edited[0].update(budget_usd="50", publish="private")
    queue.save(str(st), edited, queue.STORY_COLUMNS)
    queue.write_story(str(st), {**stale[0], "state": "done"})                       # the tick writes its own row
    rows = {r["slug"]: r for r in queue.load(str(st), queue.STORY_COLUMNS)}
    assert rows["a"]["state"] == "done" and rows["b"]["state"] == "awaiting:story"
    assert rows["a"]["budget_usd"] == "50" and rows["a"]["publish"] == "private"   # the owner's columns survive
    queue.write_story(str(st), {**base, "slug": "c", "state": "new"})
    assert [r["slug"] for r in queue.load(str(st), queue.STORY_COLUMNS)] == ["a", "b", "c"]


# ------------------------------------------------------------------ the bucket as GCS sees it: objects with generations

class _Obj:
    def __init__(self, text, generation):
        self.text, self.generation = text, generation

    @property
    def data(self) -> bytes:
        return self.text if isinstance(self.text, bytes) else self.text.encode("utf-8")


def _crc(data: bytes) -> str:
    import base64

    import google_crc32c
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode("ascii")


class _FakeBlob:
    def __init__(self, objects, key, generation):
        self._o, self.key, self.generation = objects, key, generation
        cur = objects.get(key)                        # what a listing or a look-up reports about the object
        self.name, self.size, self.crc32c = key, len(cur.data) if cur else None, _crc(cur.data) if cur else None

    def exists(self):
        return self.key in self._o

    def download_as_text(self, **kw):                 # reads through the generation it was looked up with, as GCS does
        from google.api_core.exceptions import NotFound
        cur = self._o.get(self.key)
        if self.generation and (cur is None or cur.generation != self.generation):
            raise NotFound("that generation is gone")
        if cur is None:
            raise NotFound(self.key)
        return cur.data.decode("utf-8")

    def download_to_filename(self, filename, **kw):
        from google.api_core.exceptions import NotFound
        cur = self._o.get(self.key)
        if cur is None:
            raise NotFound(self.key)
        Path(filename).write_bytes(cur.data)

    def upload_from_string(self, text, content_type=None, if_generation_match=None):
        from google.api_core.exceptions import PreconditionFailed
        cur = self._o.get(self.key)
        gen = cur.generation if cur else 0
        if if_generation_match is not None and if_generation_match != gen:
            raise PreconditionFailed("stale generation")
        self._o[self.key] = _Obj(text, gen + 1)

    def upload_from_filename(self, filename, content_type=None, **kw):
        cur = self._o.get(self.key)
        self._o[self.key] = _Obj(Path(filename).read_bytes(), (cur.generation if cur else 0) + 1)

    def delete(self, **kw):
        from google.api_core.exceptions import NotFound
        if self._o.pop(self.key, None) is None:
            raise NotFound(self.key)


class _FakeBucket:
    def __init__(self, objects, on_get=None):
        self._o, self._on_get = objects, on_get

    def get_blob(self, key):
        o = self._o.get(key)
        blob = _FakeBlob(self._o, key, o.generation) if o else None
        if self._on_get:
            self._on_get(key)
        return blob

    def blob(self, key):
        return _FakeBlob(self._o, key, 0)

    def list_blobs(self, prefix="", max_results=None):
        keys = sorted(k for k in self._o if k.startswith(prefix))
        return iter([_FakeBlob(self._o, k, self._o[k].generation) for k in (keys[:max_results] if max_results else keys)])


class _FakeClient:
    def __init__(self, objects, on_get=None):
        self._o, self._on_get = objects, on_get

    def bucket(self, name):
        return _FakeBucket(self._o, self._on_get)


def _fake_bucket(monkeypatch, on_get=None) -> dict:
    from nraiorbit.release import gcs
    objects: dict = {}
    monkeypatch.setattr(gcs, "STORAGE_FACTORY", lambda: _FakeClient(objects, on_get))
    return objects


_ROW = {"story_uri": "", "budget_usd": "1", "gates": "ask", "publish": "none", "note": "", "updated": ""}


def test_a_sheet_write_lands_only_on_the_generation_it_read_and_redoes_itself(monkeypatch):
    """The tick's claim and a save in the control room, seconds apart: the second writer's precondition fails, it
    reads the sheet again, redoes its change on top of the first writer's, and nothing is lost."""
    from nraiorbit.service import queue
    objects = _fake_bucket(monkeypatch)
    uri, key = "gs://bkt/queue/stories.csv", "queue/stories.csv"
    rows = [{**_ROW, "slug": "a", "state": "new"}, {**_ROW, "slug": "b", "state": "approved"}]
    queue.update_text(uri, lambda t: queue.csv_of(rows, queue.STORY_COLUMNS))
    assert objects[key].generation == 1
    reads = []

    def claim(text):                                      # the tick claims b; between its read and write the owner
        reads.append(text)                                # lowers a's cap from the control room
        if len(reads) == 1:
            other = queue.rows_of(text, queue.STORY_COLUMNS)
            other[0]["budget_usd"] = "50"
            _FakeBlob(objects, key, 0).upload_from_string(queue.csv_of(other, queue.STORY_COLUMNS))
        mine = queue.rows_of(text, queue.STORY_COLUMNS)
        mine[1]["state"] = "producing"
        return queue.csv_of(mine, queue.STORY_COLUMNS)
    queue.update_text(uri, claim)
    final = {r["slug"]: r for r in queue.rows_of(objects[key].text, queue.STORY_COLUMNS)}
    assert len(reads) == 2 and objects[key].generation == 3
    assert final["a"]["budget_usd"] == "50" and final["b"]["state"] == "producing"
    queue.write_story(uri, {**_ROW, "slug": "b", "state": "done"})        # the result, through the same path
    assert queue.rows_of(objects[key].text, queue.STORY_COLUMNS)[1]["state"] == "done"
    assert queue.update_text(uri, lambda t: None) == objects[key].text and objects[key].generation == 4


def test_the_control_room_edits_a_row_under_the_same_precondition(monkeypatch):
    from nraiorbit.service import queue
    from nraiorbit.ui.store import Store
    objects = _fake_bucket(monkeypatch)
    store, key = Store("gs://bkt/films"), "films/queue/stories.csv"
    seed = queue.csv_of([{**_ROW, "slug": "a", "state": "awaiting:story"}], queue.STORY_COLUMNS)
    store.update_text("queue/stories.csv", lambda t: seed, "text/csv")
    reads = []

    def approve(text):                                    # the owner approves; a tick's note lands in between
        reads.append(text)
        if len(reads) == 1:
            rows = queue.rows_of(text, queue.STORY_COLUMNS)
            rows[0]["note"] = "a tick wrote this"
            _FakeBlob(objects, key, 0).upload_from_string(queue.csv_of(rows, queue.STORY_COLUMNS))
        rows = queue.rows_of(text, queue.STORY_COLUMNS)
        rows[0]["state"] = "approved"
        return queue.csv_of(rows, queue.STORY_COLUMNS)
    store.update_text("queue/stories.csv", approve, "text/csv")
    row = queue.rows_of(objects[key].text, queue.STORY_COLUMNS)[0]
    assert len(reads) == 2 and row["state"] == "approved" and row["note"] == "a tick wrote this"
    assert store.update_text("queue/stories.csv", lambda t: None, "text/csv") == objects[key].text


def test_a_failed_restore_stops_the_tick_before_produce_keeps_the_row_and_blocks_after_three(tmp_path, monkeypatch):
    """Any set that fails to restore (clips, caches, plan files, markers, the ledger) stops the tick: a half-restored
    project would pay again or pass a gate nobody looked at. The row keeps its state (an approval is not dropped),
    the note counts the strikes, and the third in a row blocks the film so the owner hears of it."""
    from nraiorbit.ops import backup
    from nraiorbit.service import queue
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{**_ROW, "slug": "rain", "story_uri": str(tmp_path / "rain.md"), "state": "approved"}],
               queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    projects = tmp_path / "projects"
    (projects / "_template").mkdir(parents=True)
    (projects / "_template" / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    monkeypatch.setattr(backup, "sync_library", lambda *a, **k: None)
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None, **k: (4, ["inbox"]))
    ran, seen = [], {}
    kw = dict(bucket="b", produce_fn=lambda r, b: ran.append(1) or {"state": "done", "steps": {}}, learn_fn=None,
              template=projects / "_template", log=lambda *_: None)
    states = []
    for _ in range(3):
        queue.tick(str(st), str(rf), projects, **kw)
        row = queue.load(str(st), queue.STORY_COLUMNS)[0]
        states.append((row["state"], row["note"][:19]))
    assert ran == [] and not (projects / "rain" / "pipeline.yaml").exists()      # nothing run, nothing remade
    assert states == [("approved", "restore failed (1):"), ("approved", "restore failed (2):"),
                      ("blocked:restore", "restore failed (3):")]
    # the owner sets retry once the bucket is back; an empty bucket (a first run) is no failure: produce runs and
    # the backup may mirror the markers this run restored
    rows = queue.load(str(st), queue.STORY_COLUMNS)
    rows[0]["state"] = "retry"
    queue.save(str(st), rows, queue.STORY_COLUMNS)
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None, **k: (0, []))
    monkeypatch.setattr(backup, "backup", lambda root, b, log=None, mirror_reviews=False, **k: seen.update(mirror=mirror_reviews))
    queue.tick(str(st), str(rf), projects, **kw)
    assert ran == [1] and seen == {"mirror": True} and queue.load(str(st), queue.STORY_COLUMNS)[0]["state"] == "done"


def test_restore_asks_the_bucket_what_is_there_and_only_a_real_error_is_a_failure(tmp_path, monkeypatch):
    from pathlib import Path

    from nraiorbit.ops import backup
    from nraiorbit.release import gcs
    objects = _fake_bucket(monkeypatch)
    objects["film/reports/produce.json"] = _Obj("{}", 1)                  # held: a failing copy of it is a failure
    objects["film/inbox/video/a.mp4"] = _Obj("", 1)                       # held and restored fine
    objects["film/project/pipeline.yaml"] = _Obj("x: 1", 1)               # held; pricing.yaml etc. never existed
    fetched, real = [], gcs.download_blob

    def download(blob, local):
        if "/reports/" in blob.name:
            raise gcs.BucketError("503 backend error")
        fetched.append(blob.name)
        real(blob, local)
    monkeypatch.setattr(gcs, "download_blob", download)
    monkeypatch.setattr(gcs, "RUN", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gcloud with a client")))
    done, failed = backup.restore_report(tmp_path / "film", "bkt", ["reviews", "reports", "inbox", "files"],
                                         log=lambda *_: None)
    assert failed == ["reports"] and done == 2                                # absent sets never asked for
    assert fetched == ["film/inbox/video/a.mp4", "film/project/pipeline.yaml"]   # one set at a time, in set order
    assert (tmp_path / "film" / "pipeline.yaml").read_text() == "x: 1"
    # without a client to ask, only gcloud's own no-match messages mean "nothing there"
    monkeypatch.setattr(gcs, "STORAGE_FACTORY", lambda: (_ for _ in ()).throw(RuntimeError("no credentials here")))

    def rsync2(src, dst, delete=False):
        if "reviews" in str(src):
            raise gcs.BucketError("ERROR: Did not find existing container at: gs://bkt/film/reviews")
        if "reports" in str(src):
            raise gcs.BucketError("ERROR: HTTP Error 404: Not Found (a token problem, not an absence)")
    monkeypatch.setattr(gcs, "rsync", rsync2)
    monkeypatch.setattr(gcs, "copy", lambda src, dst: (_ for _ in ()).throw(
        gcs.BucketError("The following URLs matched no objects or files: gs://bkt/film/project/takes.yaml")))
    done, failed = backup.restore_report(tmp_path / "film", "bkt", ["reviews", "reports", "files"], log=lambda *_: None)
    assert failed == ["reports"] and done == 0
    # the mirror with deletes reaches the reviews folder only when asked for
    calls = []
    monkeypatch.setattr(gcs, "rsync", lambda src, dst, delete=False: calls.append((Path(src).name, delete)))
    backup.backup(tmp_path / "film", "bkt", ["reviews", "inbox"], log=lambda *_: None, mirror_reviews=True)
    assert calls == [("reviews", True), ("inbox", False)]
    calls.clear()
    backup.backup(tmp_path / "film", "bkt", ["reviews", "inbox"], log=lambda *_: None)
    assert calls == [("reviews", False), ("inbox", False)]


def test_a_sheet_rewritten_between_the_lookup_and_the_read_is_read_again(monkeypatch):
    """GCS reads a blob through the generation it was looked up with; a rewrite in between answers NotFound, which
    is just another conflict: look it up again."""
    from nraiorbit.service import queue
    hits = []

    def on_get(key):                                      # the first look-up is followed by someone else's write
        if not hits:
            hits.append(key)
            cur = objects[key]
            objects[key] = _Obj(cur.text.replace("new", "approved"), cur.generation + 1)
    objects = _fake_bucket(monkeypatch, on_get)
    uri, key = "gs://bkt/queue/stories.csv", "queue/stories.csv"
    objects[key] = _Obj(queue.csv_of([{**_ROW, "slug": "a", "state": "new"}], queue.STORY_COLUMNS), 1)
    queue.write_story(uri, {**_ROW, "slug": "a", "state": "producing", "note": "claimed"})
    row = queue.rows_of(objects[key].text, queue.STORY_COLUMNS)[0]
    assert hits == [key] and row["state"] == "producing" and row["note"] == "claimed" and objects[key].generation == 3


def test_a_reference_result_lands_on_the_row_the_tick_picked_even_when_a_link_is_queued_twice(tmp_path):
    from nraiorbit.service import queue
    rf = tmp_path / "references.csv"
    ref = {"url": "https://youtu.be/x", "title": "Ref", "learn": "score", "minutes": "", "state": "new",
           "profile_uri": "", "note": "", "updated": ""}
    queue.save(str(rf), [ref], queue.REFERENCE_COLUMNS)
    edited = queue.load(str(rf), queue.REFERENCE_COLUMNS)              # the owner adds the length mid-watch
    edited[0]["minutes"] = "150"
    queue.save(str(rf), edited, queue.REFERENCE_COLUMNS)
    queue.write_reference(str(rf), {**ref, "state": "done", "profile_uri": "p.json"})
    row = queue.load(str(rf), queue.REFERENCE_COLUMNS)[0]
    assert row["state"] == "done" and row["profile_uri"] == "p.json" and row["minutes"] == "150"
    # the same link queued again for another focus: the tick picks the pending row, and only that row is written
    again = {**ref, "learn": "trailer"}
    queue.save(str(rf), [row, again], queue.REFERENCE_COLUMNS)
    st = tmp_path / "stories.csv"
    queue.save(str(st), [], queue.STORY_COLUMNS)
    paid = []
    for _ in range(3):
        queue.tick(str(st), str(rf), tmp_path / "projects", bucket=None, produce_fn=None,
                   learn_fn=lambda url, title, what, minutes: paid.append(what) or tmp_path / f"{what}.json",
                   log=lambda *_: None)
    rows = queue.load(str(rf), queue.REFERENCE_COLUMNS)
    assert paid == ["trailer"]                                           # one watch, paid once
    assert rows[0]["profile_uri"] == "p.json" and rows[1]["state"] == "done" and rows[1]["profile_uri"].endswith("trailer.json")


def test_a_failed_restore_on_a_resumed_claim_goes_to_retry_not_back_to_producing(tmp_path, monkeypatch):
    from nraiorbit.ops import backup
    from nraiorbit.service import queue
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{**_ROW, "slug": "rain", "story_uri": str(tmp_path / "rain.md"), "state": "producing",
                          "updated": "2026-01-01T00:00:00+00:00"}], queue.STORY_COLUMNS)   # an expired claim
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    monkeypatch.setattr(backup, "sync_library", lambda *a, **k: None)
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None, **k: (0, ["cache"]))
    queue.tick(str(st), str(rf), tmp_path / "projects", bucket="b", produce_fn=None, learn_fn=None,
               log=lambda *_: None)
    row = queue.load(str(st), queue.STORY_COLUMNS)[0]
    assert row["state"] == "retry" and row["note"].startswith("restore failed (1)")
    assert queue.ready_story([row]) is row                               # the next tick tries again at once


def test_a_film_is_never_made_without_the_template_and_the_runner_finds_the_packaged_one(tmp_path, monkeypatch):
    """On Cloud Run the work volume holds films only and the image holds the template: a film made from scratch
    would name no providers and run as a free mock animatic. The tick refuses, and the CLI falls back to the
    template shipped with the package."""
    from nraiorbit import cli
    from nraiorbit.service import queue
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{**_ROW, "slug": "rain", "story_uri": str(tmp_path / "rain.md"), "state": "new"}],
               queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    ran = []
    queue.tick(str(st), str(rf), tmp_path / "work", bucket=None, produce_fn=lambda r, b: ran.append(1),
               learn_fn=None, template=tmp_path / "work" / "_template", log=lambda *_: None)
    row = queue.load(str(st), queue.STORY_COLUMNS)[0]
    assert ran == [] and row["state"] == "blocked:FileNotFoundError" and "template" in row["note"]
    assert not (tmp_path / "work" / "rain" / "pipeline.yaml").exists()
    monkeypatch.setattr(cli, "PROJECTS", tmp_path / "work")                   # no _template there, as on the runner
    packaged = cli.project_template()
    assert packaged.name == "_template" and (packaged / "pipeline.yaml").exists()
    assert "providers" in (packaged / "pipeline.yaml").read_text(encoding="utf-8")
    (tmp_path / "work" / "_template").mkdir(parents=True)
    assert cli.project_template() == tmp_path / "work" / "_template"          # the folder's own wins when present


def test_assembly_masters_again_when_the_trailers_source_was_not_restored(tmp_path):
    """A fresh container restores the finished master but not 10_edit (never backed up): the trailer is cut from
    10_edit/film_dialogue.mp4, so assembly masters again from the timeline instead of letting ffmpeg fail."""
    from nraiorbit.ops import backup
    calls = []
    paths = ProjectPaths(tmp_path)

    def master(movie):
        # by the time the film is mastered again, no old clean copy is left for the cards pass to re-card
        assert not (paths.edit / "movie_nocards.mp4").exists()
        calls.append(("master", movie.name))

    p = SimpleNamespace(p=paths, log=lambda *_: None, _animatic=lambda: True, _master=master,
                        _place_cards=lambda movie: calls.append(("cards", movie.name)), _write_edl=lambda: None,
                        _check_sync=lambda: None,
                        gate=lambda name: calls.append(("gate", name)))
    p.p.master.mkdir(parents=True)
    p.p.edit.mkdir(parents=True)
    (p.p.master / "movie.mp4").write_bytes(b"cut")
    Pipeline._assemble(p)
    assert calls == [("master", "movie.mp4"), ("cards", "movie.mp4"), ("gate", "pacing")]
    assert not (p.p.master / "movie.mp4").exists()                        # the stale master is dropped first
    calls.clear()
    (p.p.master / "movie.mp4").write_bytes(b"carded")                     # a local disk: an old clean copy is
    (p.p.edit / "movie_nocards.mp4").write_bytes(b"old clean copy")        # still there, the trailer's source gone
    Pipeline._assemble(p)
    assert calls == [("master", "movie.mp4"), ("cards", "movie.mp4"), ("gate", "pacing")]
    assert not (p.p.edit / "movie_nocards.mp4").exists()                  # dropped before the re-master (see stub)
    calls.clear()
    (p.p.master / "movie.mp4").write_bytes(b"cut")
    (p.p.edit / "film_dialogue.mp4").write_bytes(b"dialogue cut")
    Pipeline._assemble(p)
    assert calls == [("cards", "movie.mp4"), ("gate", "pacing")]           # everything there: no re-master
    # 10_edit stays out of the backup on purpose: its timeline points at clips and takes that are not backed up
    # either, and a restored timeline without them would make the render step skip and the trailer fail
    assert "edit" not in backup.FOLDERS and not any("10_edit" in str(local) for _, local, _ in backup.pairs(tmp_path, "b"))


def _fake_film(tmp_path, monkeypatch, gates):
    """produce over a stand-in pipeline: its plan and render raise every step gate until approved, the cost estimate
    is 12.50 and first frames are wanted; QA, delivery and the upload are recorded, nothing is made or paid."""
    import json as _json

    from nraiorbit.qa import film
    from nraiorbit.release import deliver
    paths = ProjectPaths(tmp_path)
    plan_gates, render_gates = ["story", "outline", "scenes", "cast", "shots"], ["render", "pacing", "trailer"]

    def run(until=None):
        for g in plan_gates + ([] if until == "shots" else render_gates):
            if not (paths.reviews / f"{g}.approved").exists():
                raise GatePending(g, tmp_path)
    from nraiorbit.costing import SpendLedger
    pipeline = SimpleNamespace(p=paths, run=run, log=lambda *_: None, _voiceless=lambda: [],
                               shots_showing_children=lambda: {}, ledger=SpendLedger(paths.reports / "cost.jsonl", None),
                               cfg={"policy": {"gates": gates, "publish": "private"}, "providers": {}})
    estimate = {"total_usd": 12.5, "video_usd": 10.0, "tts_usd": 2.5}
    monkeypatch.setattr(produce, "cost_gate", lambda p: (None, dict(estimate)))
    monkeypatch.setattr(produce, "_faces_needed", lambda p: not (paths.reviews / "faces.approved").exists())
    monkeypatch.setattr(film, "check", lambda root, log=None: {"ok": True})
    monkeypatch.setattr(deliver, "deliver", lambda *a, **k: [])
    uploads = []
    monkeypatch.setattr(produce, "publish_youtube",
                        lambda root, privacy, bucket=None: uploads.append(privacy) or {"film": {"id": "v"}})
    return pipeline, paths, uploads, _json


def test_auto_runs_every_step_but_waits_for_the_cost_and_the_youtube_upload(tmp_path, monkeypatch):
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "auto")
    report = produce.produce(pipeline, log=lambda *_: None)
    assert report["state"] == "awaiting:cost"                                    # the plan ran through on its own
    assert all((paths.reviews / f"{g}.approved").read_text().startswith("approved by policy")
               for g in ("story", "outline", "scenes", "cast", "shots"))
    assert _json.loads((paths.reviews / "cost.pending").read_text())["total_usd"] == 12.5
    assert not (paths.reviews / "cost.approved").exists() and not uploads      # nothing approved for the owner
    produce.approve(pipeline, "cost", "approved in the control room\n")
    report = produce.produce(pipeline, log=lambda *_: None)
    assert report["state"] == "awaiting:publish" and not uploads                # faces, render, cut: on their own
    assert all((paths.reviews / f"{g}.approved").exists() for g in ("render", "pacing", "trailer"))
    assert not (paths.reviews / "faces.pending").exists()
    produce.approve(pipeline, "publish", "approved in the control room\n")
    report = produce.produce(pipeline, log=lambda *_: None)
    assert report["state"] == "done" and uploads == ["private"]
    # the film check's verdict is kept where the control room's QA card reads it, as `nraiorbit qa film` keeps it
    assert _json.loads((paths.reports / "qa" / "film.json").read_text()) == {"ok": True}


def test_ask_still_stops_at_every_step_and_a_grown_estimate_is_asked_again_under_auto(tmp_path, monkeypatch):
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "ask")
    assert produce.produce(pipeline, log=lambda *_: None)["state"] == "awaiting:story"
    pipeline.cfg["policy"]["gates"] = "auto"
    assert produce.produce(pipeline, log=lambda *_: None)["state"] == "awaiting:cost"
    produce.approve(pipeline, "cost", "approved in the control room\n" + _json.dumps({"total_usd": 12.5}) + "\n")
    monkeypatch.setattr(produce, "cost_gate", lambda p: (None, {"total_usd": 30.0, "video_usd": 30.0}))
    report = produce.produce(pipeline, log=lambda *_: None)                      # the plan grew: asked again, not
    assert report["state"] == "awaiting:cost" and "grew" in report["steps"]["cost"]   # waved through by policy
    assert not uploads



def test_a_film_that_cannot_be_voiced_is_never_offered_a_cost_to_approve(tmp_path, monkeypatch):
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "auto")
    pipeline._voiceless = lambda: ["chintu", "lakshmi"]
    report = produce.produce(pipeline, log=lambda *_: None)
    assert report["state"].startswith("blocked:voices: no ElevenLabs voice for ['chintu', 'lakshmi']")
    assert not (paths.reviews / "cost.pending").exists() and not uploads


def test_a_gate_named_like_an_owner_gate_is_never_waved_through_by_auto(tmp_path, monkeypatch):
    """Whatever raises it, a cost or publish gate is the owner's: auto approves every other gate only."""
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "auto")

    def run(until=None):
        if not (paths.reviews / "publish.approved").exists():
            raise GatePending("publish", tmp_path)
    pipeline.run = run
    report = produce.produce(pipeline, log=lambda *_: None)
    assert report["state"] == "awaiting:publish" and not (paths.reviews / "publish.approved").exists()


def test_a_skipped_film_check_replaces_the_last_cuts_verdict(tmp_path, monkeypatch):
    from nraiorbit.qa import film
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "auto")
    (paths.reports / "qa").mkdir(parents=True)
    (paths.reports / "qa" / "film.json").write_text('{"ok": true}')            # the previous cut's verdict

    def broken(root, log=None):
        raise ImportError("no mediapipe")
    monkeypatch.setattr(film, "check", broken)
    for gate in ("cost", "publish"):
        produce.approve(pipeline, gate, "approved in the control room\n")
    produce.produce(pipeline, log=lambda *_: None)
    kept = _json.loads((paths.reports / "qa" / "film.json").read_text())
    assert kept["ok"] is None and kept["skipped"].startswith("ImportError")



def test_a_quota_stop_in_the_paid_stages_is_a_retry_not_a_block(tmp_path, monkeypatch):
    from nraiorbit.pipeline import PipelineBlocked
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "auto")
    produce.approve(pipeline, "cost", "approved\n")
    plan_run = pipeline.run

    def run(until=None):
        if until == "shots":
            return plan_run(until)
        raise PipelineBlocked("Gemini image (keyframe sc009_s001): 429 RESOURCE_EXHAUSTED")
    for g in ("story", "outline", "scenes", "cast", "shots"):
        produce.approve(pipeline, g)
    pipeline.run = run
    report = produce.produce(pipeline, log=lambda *_: None)
    assert report["state"] == "retry:quota" and report["steps"]["render"].startswith("quota:")


def test_a_faces_hold_from_the_video_step_survives_a_retry_and_the_video_step_looks_first(tmp_path, monkeypatch):
    """gates: ask. The video step held faces again, naming new stills: a Retry before approving stops at faces with
    that list intact; after the approval, the paid stages run with look_at_new_frames on (a still drawn then waits)."""
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "ask")
    for g in ("story", "outline", "scenes", "cast", "shots", "cost"):
        produce.approve(pipeline, g, "approved in the control room\n")
    named = "New first frames were drawn while the video was made: sc008_s002.\n"
    paths.reviews.mkdir(parents=True, exist_ok=True)
    (paths.reviews / "faces.pending").write_text(named, encoding="utf-8")
    made = []
    report = produce.produce(pipeline, keyframes=lambda args: made.append(args.slug), log=lambda *_: None)
    assert report["state"] == "awaiting:faces" and made                       # missing stills are made first
    assert (paths.reviews / "faces.pending").read_text(encoding="utf-8") == named
    assert not getattr(pipeline, "look_at_new_frames", False)                 # nothing paid this run
    produce.approve(pipeline, "faces", "approved in the control room\n")
    report = produce.produce(pipeline, keyframes=lambda args: None, log=lambda *_: None)
    assert report["state"] == "awaiting:render" and pipeline.look_at_new_frames is True


def _sheet(tmp_path, state="retry", updated="2026-10-22T05:00:00+00:00"):
    from nraiorbit.service import queue
    st = str(tmp_path / "stories.csv")
    queue.save(st, [{"slug": "rain", "story_uri": str(tmp_path / "rain.md"), "budget_usd": "40", "gates": "ask",
                     "publish": "none", "state": state, "note": "", "updated": updated},
                    {"slug": "sun", "story_uri": "x", "budget_usd": "10", "gates": "ask", "publish": "none",
                     "state": "blocked:owner", "note": "keep me", "updated": ""}], queue.STORY_COLUMNS)
    return st


def test_two_ticks_that_read_the_same_row_claim_the_film_once(tmp_path, monkeypatch):
    """monkey-saves-man-2: a tick started by hand and the scheduler's started in the same second, both read
    `retry`, and both claimed the film (each rewrite redid the claim without looking again), so every clip was
    bought twice. The claim now lands only on the row as it was read."""
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    st = _sheet(tmp_path)
    a = queue.ready_story(queue.load(st, queue.STORY_COLUMNS))
    b = queue.ready_story(queue.load(st, queue.STORY_COLUMNS))                 # both ticks read `retry`
    assert a["slug"] == b["slug"] == "rain"
    assert queue.claim_story(st, a) and a["state"] == "producing"
    assert not queue.claim_story(st, b) and b["state"] == "retry"              # the second backs off
    rows = {r["slug"]: r for r in queue.load(st, queue.STORY_COLUMNS)}
    assert rows["rain"]["state"] == "producing" and rows["rain"]["updated"] == a["updated"]
    assert rows["sun"] == {**rows["sun"], "state": "blocked:owner", "note": "keep me"}   # other rows untouched

    st = _sheet(tmp_path, "producing", "2026-01-01T00:00:00+00:00")           # a claim long expired: taken once
    c = queue.ready_story(queue.load(st, queue.STORY_COLUMNS))
    d = queue.ready_story(queue.load(st, queue.STORY_COLUMNS))
    assert queue.claim_story(st, c) and not queue.claim_story(st, d)

    st = _sheet(tmp_path)
    e = queue.ready_story(queue.load(st, queue.STORY_COLUMNS))
    rows = queue.load(st, queue.STORY_COLUMNS)
    rows[0].update(state="blocked:owner", updated=queue.now())                 # the owner stopped it since
    queue.save(st, rows, queue.STORY_COLUMNS)
    assert not queue.claim_story(st, e)
    assert queue.load(st, queue.STORY_COLUMNS)[0]["state"] == "blocked:owner"


def test_a_tick_that_read_the_row_before_another_claimed_it_leaves_the_film(tmp_path, monkeypatch):
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    st = _sheet(tmp_path)
    rf = str(tmp_path / "references.csv")
    queue.save(rf, [], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    template = tmp_path / "projects" / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    stale = queue.load(st, queue.STORY_COLUMNS)                                 # what the second tick read
    made = []

    def produce_fn(root, bucket):
        made.append(root.name)
        return {"state": "awaiting:scenes", "steps": {}}
    first = queue.tick(st, rf, tmp_path / "projects", bucket=None, produce_fn=produce_fn, learn_fn=None,
                       template=template, log=lambda *_: None)
    assert first["story"] == {"slug": "rain", "state": "awaiting:scenes"}
    real_load, logs = queue.load, []
    monkeypatch.setattr(queue, "load", lambda uri, cols: [dict(r) for r in stale] if uri == st else real_load(uri, cols))
    second = queue.tick(st, rf, tmp_path / "projects", bucket=None, produce_fn=produce_fn, learn_fn=None,
                        template=template, log=logs.append)
    assert made == ["rain"] and second["story"] is None                       # made once
    assert any(queue.CHANGED_SINCE_READ in line for line in logs)
    assert real_load(st, queue.STORY_COLUMNS)[0]["state"] == "awaiting:scenes"  # the first tick's result stands


def test_a_claim_redone_after_a_write_conflict_looks_at_the_row_again(tmp_path, monkeypatch):
    """In the bucket the claim is a guarded write: when another tick wrote first, the sheet is read again and the
    change redone. The redo must see that tick's claim and back off, not claim the film a second time."""
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    st = _sheet(tmp_path)
    mine = queue.ready_story(queue.load(st, queue.STORY_COLUMNS))
    before = (tmp_path / "stories.csv").read_text(encoding="utf-8")
    theirs = queue.load(st, queue.STORY_COLUMNS)
    theirs[0].update(state="producing", updated=queue.now())
    after = queue.csv_of(theirs, queue.STORY_COLUMNS)
    seen = []

    def update_text(uri, change, *a, **k):                                     # read, conflict, read again
        seen.append(change(before) is not None)                                # first look: it would claim
        new = change(after)                                                     # the redo, on the sheet as it is
        seen.append(new is not None)
        return after if new is None else new
    monkeypatch.setattr(queue, "update_text", update_text)
    assert not queue.claim_story("gs://b/queue/stories.csv", mine) and seen == [True, False]
    assert mine["state"] == "retry"



def _refs(tmp_path, state="new", updated="2026-10-22T05:00:00+00:00"):
    from nraiorbit.service import queue
    rf = str(tmp_path / "references.csv")
    queue.save(rf, [{"url": "https://youtu.be/x", "title": "Ref", "learn": "score", "minutes": "", "state": state,
                     "profile_uri": "", "note": "", "updated": updated},
                    {"url": "https://youtu.be/x", "title": "Ref", "learn": "camera", "minutes": "", "state": "done",
                     "profile_uri": "p.json", "note": "", "updated": ""}], queue.REFERENCE_COLUMNS)
    return rf


def test_a_reference_is_learned_once_when_two_ticks_read_it_together(tmp_path, monkeypatch):
    """A learn is paid (watching the video): the same claim as a film's, so two ticks that start together learn it
    once, the result lands on the claimed row, and a learn cut short is picked up again when its claim expires."""
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    rf = _refs(tmp_path)
    a = queue.ready_reference(queue.load(rf, queue.REFERENCE_COLUMNS))
    b = queue.ready_reference(queue.load(rf, queue.REFERENCE_COLUMNS))
    assert queue.claim_reference(rf, a) and a["state"] == "learning"
    assert not queue.claim_reference(rf, b) and b["state"] == "new"
    rows = queue.load(rf, queue.REFERENCE_COLUMNS)
    assert [r["state"] for r in rows] == ["learning", "done"]
    assert queue.ready_reference(rows) is None                                  # a fresh claim: nobody else learns
    a.update(state="done", profile_uri="score.json", note="", updated=queue.now())
    queue.write_reference(rf, a)                                                # the result: on the claimed row
    rows = queue.load(rf, queue.REFERENCE_COLUMNS)
    assert len(rows) == 2 and rows[0]["state"] == "done" and rows[0]["profile_uri"] == "score.json"
    rf = _refs(tmp_path, "learning", "2026-01-01T00:00:00+00:00")               # cut short long ago: again, once
    c = queue.ready_reference(queue.load(rf, queue.REFERENCE_COLUMNS))
    d = queue.ready_reference(queue.load(rf, queue.REFERENCE_COLUMNS))
    assert c is not None and queue.claim_reference(rf, c) and not queue.claim_reference(rf, d)


def test_a_tick_that_read_a_reference_before_another_claimed_it_does_not_learn_it(tmp_path, monkeypatch):
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    st, rf = _sheet(tmp_path, "blocked:owner"), _refs(tmp_path)
    stale = queue.load(rf, queue.REFERENCE_COLUMNS)
    watched, logs = [], []

    def learn_fn(url, title, what, minutes):
        watched.append(url)
        return tmp_path / "profile.json"
    queue.tick(st, rf, tmp_path / "projects", bucket=None, produce_fn=None, learn_fn=learn_fn, log=lambda *_: None)
    real_load = queue.load
    monkeypatch.setattr(queue, "load", lambda uri, cols: [dict(r) for r in stale] if uri == rf else real_load(uri, cols))
    second = queue.tick(st, rf, tmp_path / "projects", bucket=None, produce_fn=None, learn_fn=learn_fn, log=logs.append)
    assert watched == ["https://youtu.be/x"] and second["reference"] is None
    assert any(queue.CHANGED_SINCE_READ in line for line in logs)


def test_the_cli_never_adds_the_same_story_or_watch_twice(tmp_path, capsys):
    from types import SimpleNamespace

    from nraiorbit import cli
    from nraiorbit.service import queue
    st, rf = _sheet(tmp_path), _refs(tmp_path)
    story = dict(action="add-story", stories=st, references=rf, story_uri="s.md", budget=None, gates=None,
                 publish=None)
    with pytest.raises(SystemExit, match="already in"):
        cli.cmd_service(SimpleNamespace(**story, slug="rain"))                   # a second rain would never run
    cli.cmd_service(SimpleNamespace(**story, slug="moon"))
    assert [r["slug"] for r in queue.load(st, queue.STORY_COLUMNS)] == ["rain", "sun", "moon"]
    ref = dict(action="add-reference", stories=st, references=rf, title=None, minutes=None)
    with pytest.raises(SystemExit, match="already queued"):
        cli.cmd_service(SimpleNamespace(**ref, url="https://youtu.be/x", learn="camera"))
    cli.cmd_service(SimpleNamespace(**ref, url="https://youtu.be/y", learn=None))
    assert [(r["url"], r["learn"]) for r in queue.load(rf, queue.REFERENCE_COLUMNS)][-1] == ("https://youtu.be/y", "all")


def test_a_reference_result_lands_on_the_claimed_row_even_after_the_owner_renamed_it(tmp_path, monkeypatch):
    """The owner retitled the reference while it was being learned, and the same link waits in another row for a
    different focus: the result goes to the claimed (learning) row, never to the waiting one."""
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    rf = str(tmp_path / "references.csv")
    queue.save(rf, [{"url": "https://youtu.be/x", "title": "Ref", "learn": "score", "minutes": "", "state": "new",
                     "profile_uri": "", "note": "", "updated": "t0"},
                    {"url": "https://youtu.be/x", "title": "Ref", "learn": "camera", "minutes": "", "state": "new",
                     "profile_uri": "", "note": "", "updated": "t0"}], queue.REFERENCE_COLUMNS)
    a = queue.ready_reference(queue.load(rf, queue.REFERENCE_COLUMNS))
    assert queue.claim_reference(rf, a)
    rows = queue.load(rf, queue.REFERENCE_COLUMNS)
    rows[0]["title"] = "Renamed"                                                # the owner, mid-learn
    queue.save(rf, rows, queue.REFERENCE_COLUMNS)
    a.update(state="done", profile_uri="score.json", updated=queue.now())
    queue.write_reference(rf, a)
    rows = queue.load(rf, queue.REFERENCE_COLUMNS)
    assert [(r["learn"], r["state"]) for r in rows] == [("score", "done"), ("camera", "new")]


def test_a_reference_result_goes_to_the_row_its_tick_claimed_among_identical_rows(tmp_path, monkeypatch):
    from nraiorbit.service import queue
    monkeypatch.delenv("NRAIORBIT_CLAIM_HOURS", raising=False)
    rf = str(tmp_path / "references.csv")
    same = {"url": "https://youtu.be/x", "title": "Ref", "learn": "score", "minutes": "", "profile_uri": "",
            "note": ""}
    queue.save(rf, [{**same, "state": "new", "updated": "t1"}, {**same, "state": "new", "updated": "t2"}],
               queue.REFERENCE_COLUMNS)                                         # two identical rows, by hand
    rows = queue.load(rf, queue.REFERENCE_COLUMNS)
    second = rows[1]
    assert queue.claim_reference(rf, second)                                     # a tick took the second one
    second.update(state="done", profile_uri="score.json", updated=queue.now())
    queue.write_reference(rf, second)
    assert [r["state"] for r in queue.load(rf, queue.REFERENCE_COLUMNS)] == ["new", "done"]


def test_a_film_already_on_youtube_is_never_asked_for_again(tmp_path):
    """A run over a finished film (to cut its reels) found the publish approval spent by the upload and asked again;
    the record says this very film and trailer are up, so it is not. A new cut is asked for."""
    root = tmp_path / "film"
    (root / "11_master").mkdir(parents=True)
    (root / "reports").mkdir()
    movie, trailer = root / "11_master" / "movie.mp4", root / "11_master" / "trailer.mp4"
    movie.write_bytes(b"the film")
    assert not produce.published_already(root)
    (root / "reports" / "youtube.json").write_text(json.dumps({"film": {"id": "F", "file": produce.fingerprint(movie)}}))
    assert produce.published_already(root)
    trailer.write_bytes(b"the trailer")                                     # a trailer that never went up
    assert not produce.published_already(root)
    (root / "reports" / "youtube.json").write_text(json.dumps({
        "film": {"id": "F", "file": produce.fingerprint(movie)}, "trailer": {"id": "T", "file": produce.fingerprint(trailer)}}))
    assert produce.published_already(root)
    movie.write_bytes(b"the film, re-cut")
    assert not produce.published_already(root)


def test_the_reels_follow_the_film_to_youtube_when_its_shorts_are_automatic(tmp_path, monkeypatch):
    """The owner approves the film's upload; with Shorts on automatic the run asks for its reels right after,
    with the film's visibility, under that same approval. A failed upload asks for nothing."""
    from nraiorbit.service import shorts
    pipeline, paths, uploads, _json = _fake_film(tmp_path, monkeypatch, "auto")
    asked = []
    monkeypatch.setattr(shorts, "after_film_upload", lambda root, bucket, cfg: asked.append("asked")
                        or "asked for automatically (private); they go up when this run ends")
    for g in ("cost", "publish"):
        produce.approve(pipeline, g, "approved in the control room\n")
    monkeypatch.setattr(produce, "published_already", lambda root: False)    # the upload failed: nothing is up
    monkeypatch.setattr(produce, "publish_youtube", lambda root, privacy, bucket=None: (_ for _ in ()).throw(
        RuntimeError("quotaExceeded")))
    report = produce.produce(pipeline, log=lambda *_: None)
    assert report["steps"]["publish"].startswith("skipped: RuntimeError") and asked == [] and "shorts" not in report["steps"]
    produce.approve(pipeline, "publish", "approved in the control room\n")
    def upload(root, privacy, bucket=None):
        (root / "reports").mkdir(parents=True, exist_ok=True)
        (root / "reports" / "youtube.json").write_text(json.dumps({"film": {"id": "F", "privacy": privacy}}))
        return {"film": {"id": "F"}}
    monkeypatch.setattr(produce, "publish_youtube", upload)
    monkeypatch.setattr(produce, "published_already", lambda root: True)
    report = produce.produce(pipeline, log=lambda *_: None)
    assert asked == ["asked"] and report["steps"]["shorts"].startswith("asked for automatically")
    assert not (paths.reviews / "publish.approved").exists()                 # spent by the upload
    report = produce.produce(pipeline, log=lambda *_: None)                  # a later run: the film is up already
    assert report["state"] == "done" and asked == ["asked", "asked"]         # (the job uploads nothing twice)
