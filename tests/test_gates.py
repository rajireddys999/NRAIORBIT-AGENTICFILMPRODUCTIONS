"""The review gates as files: the step gates fire only when a project lists them, and the runner's own gates
(cost, faces, publish) leave the same pending marker a pipeline gate does, so an approval from the control room
or the queue sheet reaches the next run."""

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
    def restore_report(root, b, log=None):                # bucket -> empty disk, nothing ever missing or failing
        if (bucket / root.name).exists():
            shutil.copytree(bucket / root.name, root, dirs_exist_ok=True)
        return 1, []
    monkeypatch.setattr(backup, "restore_report", restore_report)
    monkeypatch.setattr(backup, "backup", lambda root, b, log=None, mirror_reviews=False:
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
    sent = []
    monkeypatch.setattr(youtube, "upload", lambda video, meta, creds, captions=None, thumbnail=None:
                        sent.append(video.name) or f"id-{len(sent)}")
    out = produce.publish_youtube(root, "private")
    assert sent == ["movie.mp4", "trailer.mp4"] and out["film"]["id"] == "id-1" and out["trailer"]["id"] == "id-2"
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


class _FakeBlob:
    def __init__(self, objects, key, generation):
        self._o, self.key, self.generation = objects, key, generation

    def exists(self):
        return self.key in self._o

    def download_as_text(self):                       # reads through the generation it was looked up with, as GCS does
        from google.api_core.exceptions import NotFound
        cur = self._o.get(self.key)
        if self.generation and (cur is None or cur.generation != self.generation):
            raise NotFound("that generation is gone")
        return cur.text

    def upload_from_string(self, text, content_type=None, if_generation_match=None):
        from google.api_core.exceptions import PreconditionFailed
        cur = self._o.get(self.key)
        gen = cur.generation if cur else 0
        if if_generation_match is not None and if_generation_match != gen:
            raise PreconditionFailed("stale generation")
        self._o[self.key] = _Obj(text, gen + 1)


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
        keys = [k for k in self._o if k.startswith(prefix)]
        return iter(keys[:max_results] if max_results else keys)


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
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None: (4, ["inbox"]))
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
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None: (0, []))
    monkeypatch.setattr(backup, "backup", lambda root, b, log=None, mirror_reviews=False: seen.update(mirror=mirror_reviews))
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
    synced = []

    def rsync(src, dst, delete=False):
        if "reports" in str(src):
            raise gcs.BucketError("gcloud storage rsync failed: 503 backend error")
        synced.append(Path(str(src)).name)
    monkeypatch.setattr(gcs, "rsync", rsync)
    monkeypatch.setattr(gcs, "copy", lambda src, dst: synced.append(Path(str(src)).name))
    done, failed = backup.restore_report(tmp_path / "film", "bkt", ["reviews", "reports", "inbox", "files"],
                                         log=lambda *_: None)
    assert failed == ["reports"] and done == 2 and synced == ["inbox", "pipeline.yaml"]   # absent sets never asked for
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
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None: (0, ["cache"]))
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
    """A fresh container restores the finished master but, from an older backup, not 10_edit: the trailer is cut
    from 10_edit/film_dialogue.mp4, so assembly masters again from the timeline instead of letting ffmpeg fail."""
    from nraiorbit.ops import backup
    calls = []
    p = SimpleNamespace(p=ProjectPaths(tmp_path), log=lambda *_: None, _animatic=lambda: True,
                        _master=lambda movie: calls.append(("master", movie.name)),
                        _place_cards=lambda movie: calls.append(("cards", movie.name)),
                        gate=lambda name: calls.append(("gate", name)))
    p.p.master.mkdir(parents=True)
    p.p.edit.mkdir(parents=True)
    (p.p.master / "movie.mp4").write_bytes(b"cut")
    Pipeline._assemble(p)
    assert calls == [("master", "movie.mp4"), ("cards", "movie.mp4"), ("gate", "pacing")]
    assert not (p.p.master / "movie.mp4").exists()                        # the stale master is dropped first
    calls.clear()
    (p.p.master / "movie.mp4").write_bytes(b"cut")
    (p.p.edit / "film_dialogue.mp4").write_bytes(b"dialogue cut")
    Pipeline._assemble(p)
    assert calls == [("cards", "movie.mp4"), ("gate", "pacing")]           # everything there: no re-master
    # and the edit folder now travels with the project, so the next restore brings it back
    assert ("folder", tmp_path / "10_edit", "gs://b/" + tmp_path.name + "/10_edit") in backup.pairs(tmp_path, "b", ["edit"])
    assert "edit" in backup.DEFAULT_SETS
