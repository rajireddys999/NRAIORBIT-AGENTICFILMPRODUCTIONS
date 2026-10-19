"""Live progress: the two records the runner keeps in the bucket while it works (the film's phase and last line, the
execution's doings), throttled and failing soft, wired through the tick, produce and the pipeline; and the runner's
project template, which must be the repository's when the projects folder has none."""

import json
import os

import pytest
import yaml

from nraiorbit import cli
from nraiorbit.config import REPO_ROOT, load_pipeline_config
from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
from nraiorbit.pipeline import STAGES, Pipeline, ProjectPaths
from nraiorbit.service import produce, progress, queue

T0 = 1_800_000_000.0
RUN = "queue/running/ex1.json"
FILM = "rain/reports/progress.json"


class Bucket:
    """Objects by path, as text, with every put and delete in order."""

    def __init__(self, fail=False):
        self.objects, self.calls, self.fail = {}, [], fail

    def upload(self, path, text):
        if self.fail:
            raise OSError("bucket unreachable")
        self.objects[path] = text
        self.calls.append(("put", path, json.loads(text).get("doing")))

    def remove(self, path):
        if self.fail:
            raise OSError("bucket unreachable")
        self.objects.pop(path, None)
        self.calls.append(("del", path, None))

    def list(self, prefix):
        return {p: t for p, t in self.objects.items() if p.startswith(prefix)}

    def record(self, path):
        return json.loads(self.objects[path])

    def puts(self, path):
        return sum(1 for c in self.calls if c[:2] == ("put", path))


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _tracker(bucket, clock, log=None):
    return progress.Tracker("b", execution="ex1", uploader=bucket.upload, remover=bucket.remove, lister=bucket.list,
                            clock=clock, log=log or (lambda *_: None))


# ------------------------------------------------------------------------------------------------- the records

def test_both_records_have_the_shared_shape_and_paths(tmp_path):
    bucket, clock = Bucket(), Clock()
    t = _tracker(bucket, clock)
    t.start("story", slug="rain")
    assert bucket.record(RUN) == {"execution": "ex1", "started": progress.stamp(T0), "updated": progress.stamp(T0),
                                  "doing": "story", "slug": "rain"}
    clock.t += 60
    film = t.film("rain", tmp_path / "rain")
    film.phase("scenes")
    rec = bucket.record(FILM)
    assert rec == {"slug": "rain", "execution": "ex1", "state": "running", "phase": "scenes",
                   "phase_started": progress.stamp(T0 + 60), "started": progress.stamp(T0 + 60),
                   "updated": progress.stamp(T0 + 60), "last_line": "", "lines": 0}
    assert json.loads((tmp_path / "rain" / "reports" / "progress.json").read_text()) == rec   # the backup carries it
    run = bucket.record(RUN)
    assert run["doing"] == "story" and run["slug"] == "rain" and run["phase"] == "scenes"
    assert run["updated"] == progress.stamp(T0 + 60) and run["started"] == progress.stamp(T0)
    t.start("library-backup")                                               # every write is the whole record
    assert set(bucket.record(RUN)) == {"execution", "started", "updated", "doing"}


def test_lines_go_up_at_most_every_15_seconds_and_a_phase_at_once(tmp_path):
    bucket, clock = Bucket(), Clock()
    t = _tracker(bucket, clock)
    t.start("story", slug="rain")
    film = t.film("rain", tmp_path / "rain")
    film.phase("scenes")
    assert bucket.puts(FILM) == 1
    clock.t += 20
    film.line("  sc001: 4 lines\n")
    clock.t += 5
    film.line("sc002: 3 lines")                                             # two lines within 15 s: one upload
    film.line("   ")                                                        # a blank line is not a line
    assert bucket.puts(FILM) == 2 and bucket.record(FILM)["last_line"] == "sc001: 4 lines"
    clock.t += 1
    film.phase("cast")                                                      # a phase change: at once, with the line
    rec = bucket.record(FILM)
    assert bucket.puts(FILM) == 3 and rec["phase"] == "cast" and rec["last_line"] == "sc002: 3 lines"
    assert rec["lines"] == 2 and rec["phase_started"] == progress.stamp(T0 + 26)
    assert bucket.record(RUN)["last_line"] == "sc002: 3 lines" and bucket.puts(RUN) == 4


def test_finish_keeps_the_state_head_and_close_removes_the_running_record_once(tmp_path):
    assert [progress.state_head(s) for s in ("done", "awaiting:cost", "blocked:no cap", "failed")] == \
        ["done", "awaiting", "blocked", "failed"]
    bucket, clock = Bucket(), Clock()
    t = _tracker(bucket, clock)
    t.start("story", slug="rain")
    film = t.film("rain", tmp_path / "rain")
    film.finish("blocked:estimate $12.00 + spent $3.00 is over the cap")
    assert bucket.record(FILM)["state"] == "blocked"
    film.finish("awaiting:scenes")
    assert bucket.record(FILM)["state"] == "awaiting"
    t.close()
    t.close()
    assert RUN not in bucket.objects and [c for c in bucket.calls if c[0] == "del"] == [("del", RUN, None)]
    assert FILM in bucket.objects                                           # the film's record stays for the page
    quiet = _tracker(Bucket(), clock)
    quiet.close()                                                           # nothing was written: nothing to remove
    assert quiet.record is None


def test_the_control_room_reads_the_records_as_the_runner_writes_them(tmp_path):
    """The seam: the runner's uploads, landed in a folder shaped like the bucket, are what the film page and the
    Runner table read, so a renamed field on either side fails here rather than on the owner's screen."""
    from nraiorbit.ui import films
    from nraiorbit.ui import runner as runner_mod
    from nraiorbit.ui.store import Store
    media = tmp_path / "bucket"

    def upload(path, text):
        (media / path).parent.mkdir(parents=True, exist_ok=True)
        (media / path).write_text(text, encoding="utf-8")
    t = progress.Tracker("b", execution="ex1", uploader=upload, remover=lambda path: (media / path).unlink(),
                         lister=lambda prefix: {}, log=lambda *_: None)
    t.start("story", slug="rain")
    film = t.film("rain", tmp_path / "projects" / "rain")
    film.line("sc010: 3 lines")
    film.phase("scenes")
    store = Store(str(media))
    doing = runner_mod.doings(store)["ex1"]
    assert (doing["doing"], doing["slug"], doing["phase"], doing["last_line"]) == ("story", "rain", "scenes",
                                                                                   "sc010: 3 lines")
    row = {"slug": "rain", "state": "producing", "gates": "ask", "publish": "none", "budget_usd": "40"}
    d = films.detail(store, "rain", row, {"ex1"})
    assert d["next_stage"] == "scenes" and d["stages"]["scenes"]["status"] == "executing"
    assert d["live"]["execution"] == "ex1" and "last: sc010: 3 lines" in d["stages"]["scenes"]["text"]
    film.finish("awaiting:scenes")
    t.close()
    assert films.detail(store, "rain", row, {"ex1"})["live"] is None and runner_mod.doings(store) == {}


def test_upload_failures_are_swallowed_and_logged_once(tmp_path):
    logs = []
    t = _tracker(Bucket(fail=True), Clock(), log=logs.append)
    t.start("story", slug="rain")
    film = t.film("rain", tmp_path / "rain")
    film.phase("outline")
    film.line("a line")
    film.finish("done")
    t.close()
    assert len(logs) == 1 and "OSError" in logs[0] and "the run goes on" in logs[0]
    assert json.loads((tmp_path / "rain" / "reports" / "progress.json").read_text())["state"] == "done"
    # the project's own copy cannot be written (reports/ is a file): logged once, the bucket still hears it
    (tmp_path / "blocked").mkdir()
    (tmp_path / "blocked" / "reports").write_text("not a folder")
    logs.clear()
    bucket = Bucket()
    film = _tracker(bucket, Clock(), log=logs.append).film("rain", tmp_path / "blocked")
    film.phase("outline")
    film.finish("done")
    assert len(logs) == 1 and "FileExistsError" in logs[0] and bucket.record(FILM)["state"] == "done"


def test_without_a_bucket_only_the_local_file_is_written(tmp_path, monkeypatch):
    def boom(*a):
        raise AssertionError("nothing goes to a bucket")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "nraiorbit-runner-abc")
    t = progress.Tracker(None, uploader=boom, remover=boom, lister=boom, clock=Clock())
    assert t.execution == "nraiorbit-runner-abc"
    t.start("story", slug="rain")
    t.film("rain", tmp_path / "rain").phase("cast")
    t.close()
    assert json.loads((tmp_path / "rain" / "reports" / "progress.json").read_text())["phase"] == "cast"
    monkeypatch.delenv("CLOUD_RUN_EXECUTION")
    assert progress.Tracker(None).execution == f"local-{os.getpid()}"


def test_start_sweeps_running_records_left_behind_over_seven_hours_ago():
    bucket, clock = Bucket(), Clock()
    bucket.objects.update({
        "queue/running/old.json": json.dumps({"execution": "old", "updated": progress.stamp(T0 - 8 * 3600)}),
        "queue/running/naive.json": json.dumps({"execution": "naive", "updated": "2020-01-01T00:00:00"}),
        "queue/running/fresh.json": json.dumps({"execution": "fresh", "updated": progress.stamp(T0 - 3600)}),
        "queue/running/junk.json": "not json",
        "rain/reports/progress.json": json.dumps({"updated": progress.stamp(T0 - 9 * 3600)}),
    })
    _tracker(bucket, clock).start("library-restore")
    assert set(bucket.objects) == {"queue/running/fresh.json", "queue/running/junk.json", "rain/reports/progress.json",
                                   RUN}

    def broken(prefix):
        raise OSError("listing refused")
    t = progress.Tracker("b", execution="ex1", uploader=bucket.upload, remover=bucket.remove, lister=broken,
                         clock=clock, log=lambda *_: None)
    t.start("library-restore")                                              # a failed sweep is ignored
    assert bucket.record(RUN)["doing"] == "library-restore"


def test_the_default_storage_calls_one_client_and_a_missing_record_is_fine(monkeypatch):
    pytest.importorskip("google.api_core")
    from google.api_core.exceptions import NotFound

    from nraiorbit.release import gcs
    sent, made, kws = [], [], []

    class Blob:
        def __init__(self, name):
            self.name = name

        def upload_from_string(self, text, content_type=None, **kw):
            sent.append((self.name, content_type, json.loads(text)["doing"]))
            kws.append(kw)

        def delete(self, **kw):
            kws.append(kw)
            raise NotFound("already gone")

        def download_as_text(self, **kw):
            return json.dumps({"updated": progress.stamp(T0)})

    class FakeBucket:
        def blob(self, name):
            return Blob(name)

        def list_blobs(self, prefix=None, **kw):
            return [Blob(prefix + "other.json")]

    class Client:
        def bucket(self, name):
            made.append(name)
            return FakeBucket()

    monkeypatch.setattr(gcs, "STORAGE_FACTORY", Client)
    t = progress.Tracker("media", execution="ex1", clock=Clock(), log=lambda *_: None)
    t.start("story", slug="rain")
    t.start("library-backup")
    t.close()
    assert sent == [(RUN, "application/json", "story"), (RUN, "application/json", "library-backup")]
    assert made == ["media"] and not t._warned                              # one client; NotFound is not a failure
    # one short attempt each: a slow bucket must not hold the film up for the client's default retry window
    assert [kw.get("timeout") for kw in kws] == [progress.TIMEOUT_S] * 3
    assert kws[0]["retry"] is None and kws[1]["retry"] is None


# ---------------------------------------------------------------------------------------------- the tick

def _queues(tmp_path):
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "rain", "story_uri": str(tmp_path / "rain.md"), "budget_usd": "40", "gates": "ask",
                          "publish": "none", "state": "new", "note": "", "updated": ""}], queue.STORY_COLUMNS)
    queue.save(str(rf), [{"url": "https://www.youtube.com/watch?v=abc123xyz", "title": "Ref", "learn": "score",
                          "state": "new", "profile_uri": "", "note": "", "updated": ""}], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    template = tmp_path / "projects" / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    return str(st), str(rf), template


@pytest.fixture
def fake_bucket(monkeypatch):
    from nraiorbit.ops import backup
    bucket, clock = Bucket(), Clock()
    monkeypatch.setattr(backup, "sync_library", lambda *a, **k: None)
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None: (0, []))
    monkeypatch.setattr(backup, "backup", lambda root, b, log=None, mirror_reviews=False: 1)
    monkeypatch.setattr(queue, "TRACKER_FACTORY", lambda b, log=print: _tracker(bucket, clock, log))
    return bucket


def test_the_tick_records_its_doings_in_order_and_removes_them_when_it_exits(tmp_path, fake_bucket):
    st, rf, template = _queues(tmp_path)
    heard = []

    def produce_fn(root, bucket, progress=None):
        heard.append(progress)
        progress.phase("scenes")
        progress.finish("awaiting:scenes")
        return {"state": "awaiting:scenes", "steps": {}}

    out = queue.tick(st, rf, tmp_path / "projects", bucket="b", produce_fn=produce_fn,
                     learn_fn=lambda url, title, what, minutes: tmp_path / "profile.json", template=template,
                     log=lambda *_: None)
    assert out["story"] == {"slug": "rain", "state": "awaiting:scenes"}
    doings = [c[2] for c in fake_bucket.calls if c[:2] == ("put", RUN)]
    assert doings[:3] == ["library-restore", "reference", "story"] and doings[-1] == "library-backup"
    assert fake_bucket.calls[-1] == ("del", RUN, None) and RUN not in fake_bucket.objects
    assert isinstance(heard[0], progress.FilmProgress)
    rec = fake_bucket.record(FILM)
    assert rec["phase"] == "scenes" and rec["state"] == "awaiting" and rec["execution"] == "ex1"
    assert (tmp_path / "projects" / "rain" / "reports" / "progress.json").exists()


def test_a_two_argument_produce_fn_gets_no_progress_and_a_failure_is_recorded(tmp_path, fake_bucket):
    st, rf, template = _queues(tmp_path)
    queue.tick(st, rf, tmp_path / "projects", bucket="b",
               produce_fn=lambda root, bucket: {"state": "done", "steps": {"publish": "none"}},
               learn_fn=lambda *a: tmp_path / "p.json", template=template, log=lambda *_: None)
    assert FILM not in fake_bucket.objects and fake_bucket.calls[-1] == ("del", RUN, None)
    assert "story" in [c[2] for c in fake_bucket.calls]

    rows = queue.load(st, queue.STORY_COLUMNS)
    rows[0]["state"] = "retry"
    queue.save(st, rows, queue.STORY_COLUMNS)

    def boom(root, bucket, progress=None):
        progress.phase("render")
        raise RuntimeError("credits are out")
    out = queue.tick(st, rf, tmp_path / "projects", bucket="b", produce_fn=boom, learn_fn=None, template=template,
                     log=lambda *_: None)
    assert out["story"]["state"] == "blocked:RuntimeError"
    assert fake_bucket.record(FILM)["state"] == "failed" and RUN not in fake_bucket.objects


def test_a_tick_that_raises_still_removes_its_running_record(tmp_path, fake_bucket):
    """A tick that dies on its way (here the stories sheet cannot be read) must not leave its record behind: the
    board would show it live until Cloud Run ends it, and "left behind" for hours after."""
    st, rf, template = _queues(tmp_path)
    with pytest.raises(IsADirectoryError):
        queue.tick(str(tmp_path), rf, tmp_path / "projects", bucket="b", produce_fn=None,
                   learn_fn=lambda *a: tmp_path / "p.json", template=template, log=lambda *_: None)
    assert "reference" in [c[2] for c in fake_bucket.calls]
    assert RUN not in fake_bucket.objects and fake_bucket.calls[-1] == ("del", RUN, None)


def test_a_tick_with_nothing_to_do_says_idle_and_one_without_a_bucket_records_nothing(tmp_path, fake_bucket,
                                                                                      monkeypatch):
    st, rf, template = _queues(tmp_path)
    queue.save(st, [], queue.STORY_COLUMNS)
    queue.save(rf, [], queue.REFERENCE_COLUMNS)
    queue.tick(st, rf, tmp_path / "projects", bucket="b", produce_fn=None, learn_fn=None, log=lambda *_: None)
    assert [c[2] for c in fake_bucket.calls if c[0] == "put"] == ["library-restore", "idle"]
    assert fake_bucket.calls[-1] == ("del", RUN, None)

    def no_tracker(*a, **k):
        raise AssertionError("no bucket, no running record")
    monkeypatch.setattr(queue, "TRACKER_FACTORY", no_tracker)
    st, rf, template = _queues(tmp_path / "local")
    seen = []
    queue.tick(st, rf, tmp_path / "local" / "projects", bucket=None,
               produce_fn=lambda root, bucket, progress=None: seen.append(progress) or {"state": "done", "steps": {}},
               learn_fn=lambda *a: tmp_path / "p.json", template=template, log=lambda *_: None)
    assert seen == [None]


# ---------------------------------------------------------------------------------------- produce, pipeline

class Heard:
    def __init__(self):
        self.calls = []

    def phase(self, name):
        self.calls.append(("phase", name))

    def line(self, text):
        self.calls.append(("line", text))

    def finish(self, state):
        self.calls.append(("finish", state))


class FakePipeline:
    """What produce touches: paths, config, run() (logging each stage, then the hook, as Pipeline.run does), log."""

    def __init__(self, root):
        self.p = ProjectPaths(root)
        self.cfg = {"providers": {"video": "mock"}, "policy": {"gates": "auto", "publish": "none"}}
        self.log, self.on_stage = (lambda *_: None), None

    def run(self, until=None, force=()):
        for stage in STAGES:
            self.log(f"[{stage}]")
            if self.on_stage is not None:
                self.on_stage(stage)
            if stage == until:
                return


def test_produce_reports_every_stage_and_step_then_the_state(tmp_path, monkeypatch):
    from nraiorbit.ops import backup
    from nraiorbit.qa import film
    from nraiorbit.release import deliver
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(film, "check", lambda root, log=print: log("film check: ok") or {"ok": True})
    monkeypatch.setattr(deliver, "deliver", lambda root, bucket, **k: [])
    monkeypatch.setattr(backup, "backup", lambda root, bucket, log=print: 2)
    heard, printed = Heard(), []
    p = FakePipeline(tmp_path / "rain")
    report = produce.produce(p, bucket="b", log=printed.append, progress=heard)
    assert report["state"] == "done"
    phases = [n for k, n in heard.calls if k == "phase"]
    plan = STAGES[:STAGES.index("shots") + 1]
    assert phases == plan + ["cost", "faces"] + STAGES + ["qa", "deliver", "publish", "backup"]
    assert heard.calls[-1] == ("finish", "done")
    lines = [n for k, n in heard.calls if k == "line"]
    assert "[story_bible]" in lines and "film check: ok" in lines and lines[-1] == "produce: done"
    assert "  produce: done" in printed                                    # the original log still hears it

    quiet = FakePipeline(tmp_path / "quiet")
    produce.produce(quiet, log=lambda *_: None)                             # no progress: nothing is wired
    assert quiet.on_stage is None


def test_pipeline_run_calls_on_stage_once_per_stage_in_order(tmp_path):
    root = tmp_path / "demo"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    (root / "pipeline.yaml").write_text(yaml.safe_dump(DEMO_CONFIG, sort_keys=False), encoding="utf-8")
    events = []
    p = Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda text: events.append(("log", text)))
    assert p.on_stage is None
    p.on_stage = lambda stage: events.append(("stage", stage))
    p.run(until="outline")
    assert [n for k, n in events if k == "stage"] == ["story_bible", "outline"]
    assert events.index(("log", "[outline]")) < events.index(("stage", "outline"))


# ------------------------------------------------------------------------------------------ the template

def test_the_runner_makes_films_from_the_repository_template_when_its_projects_have_none(tmp_path, monkeypatch):
    projects = tmp_path / "work" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(cli, "PROJECTS", projects)
    assert cli.template_dir() == REPO_ROOT / "projects" / "_template"
    row = {"slug": "rain", "budget_usd": "40", "gates": "ask", "publish": "none"}
    root = queue.make_project(projects, cli.template_dir(), row, "A ferryman and a flood.")
    expected = yaml.safe_load((REPO_ROOT / "projects" / "_template" / "pipeline.yaml").read_text())["providers"]
    assert yaml.safe_load((root / "pipeline.yaml").read_text())["providers"]["video"] == expected["video"]
    (projects / "_template").mkdir()
    assert cli.template_dir() == projects / "_template"                     # a projects folder's own template wins
