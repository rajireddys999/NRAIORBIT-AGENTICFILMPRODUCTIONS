"""The control room against a local store shaped like the bucket: board, film page, approvals, the story form, the
reference queue, and the allow-list."""

import csv
import io
import json
import struct
import zlib

import pytest
import yaml

from nraiorbit.demo import OUTLINE, SCENES, SHOTS, STORY_BIBLE
from nraiorbit.schemas import Outline, SceneScript, ShotList, StoryBible
from nraiorbit.service import queue

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nraiorbit.ui import app as app_mod  # noqa: E402
from nraiorbit.ui import films  # noqa: E402
from nraiorbit.ui import runner as runner_mod  # noqa: E402
from nraiorbit.ui.app import Settings, create_app  # noqa: E402

IAP = "X-Goog-IAP-JWT-Assertion"          # tests stand in for IAP: the assertion is the e-mail, verified by a stub


@pytest.fixture(autouse=True)
def fake_iap(monkeypatch):
    monkeypatch.setattr(app_mod, "verify_assertion",
                        lambda token: {"iss": "https://cloud.google.com/iap", "email": token} if token != "bad" else None)
from nraiorbit.ui.store import Store  # noqa: E402


def png(w=4, h=4):
    raw = b"".join(b"\x00" + b"\x80\x40\x20" * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def csv_text(rows, columns):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


@pytest.fixture
def root(tmp_path):
    """A bucket-shaped folder with one film (demo plan files, a frame, a clip, a cost ledger, a pending faces gate)."""
    slug = "demo"
    base = tmp_path / "bucket"
    proj = base / slug
    (proj / "project" / "input").mkdir(parents=True)
    bible = StoryBible.model_validate(STORY_BIBLE)
    (proj / "project" / "01_story_bible.json").write_text(bible.model_dump_json(indent=1), encoding="utf-8")
    outline = Outline.model_validate(OUTLINE)
    (proj / "project" / "02_outline.json").write_text(outline.model_dump_json(indent=1), encoding="utf-8")
    (proj / "project" / "pipeline.yaml").write_text(yaml.safe_dump({"film": {"runtime_minutes": 3}, "budget": {"max_usd": 40},
                                                                     "providers": {"video": "veo", "music": "lyria"},
                                                                     "policy": {"gates": "ask", "publish": "private"}}))
    (proj / "project" / "input" / "story.md").write_text("A ferryman and a flood.", encoding="utf-8")
    (proj / "project" / "cast.lock.yaml").write_text(yaml.safe_dump({"lock_version": 1, "characters": {
        c.id: {"character_asset": f"{c.id}_x", "voice_asset": None, "costumes": {}} for c in bible.characters}}))
    (proj / "03_scenes").mkdir()
    (proj / "04_shots").mkdir()
    for sc in outline.scenes:
        if sc.scene_id in SCENES:
            script = SceneScript.model_validate({"scene_id": sc.scene_id, "heading": sc.heading, "synopsis": sc.synopsis,
                                                 "rasa_primary": sc.rasa_primary, "intensity": sc.intensity,
                                                 "blocks": SCENES[sc.scene_id]})
            (proj / "03_scenes" / f"{sc.scene_id}.json").write_text(script.model_dump_json(indent=1), encoding="utf-8")
        if sc.scene_id in SHOTS:
            sl = ShotList.model_validate({"scene_id": sc.scene_id, "shots": SHOTS[sc.scene_id]})
            (proj / "04_shots" / f"{sc.scene_id}.json").write_text(sl.model_dump_json(indent=1), encoding="utf-8")
    first = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0].shot_id
    (proj / "06_keyframes" / first).mkdir(parents=True)
    (proj / "06_keyframes" / first / "take_1.png").write_bytes(png())
    (proj / "inbox" / "video").mkdir(parents=True)
    (proj / "inbox" / "video" / f"{first}.mp4").write_bytes(b"")
    (proj / "reports").mkdir()
    ledger = [{"provider": "gemini_image", "usd": 0.15}, {"provider": "veo", "usd": 1.2}]
    (proj / "reports" / "cost.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger))
    (proj / "reports" / "produce.json").write_text(json.dumps({
        "slug": slug, "state": "awaiting:faces",
        "steps": {"plan": "done", "faces": "awaiting:faces",
                  "estimate": {"total_usd": 12.5, "video_usd": 10.0, "lipsync_usd": 2.5}},
        "ledger_usd": 1.35, "started": "2026-10-17T10:00:00+00:00", "finished": "2026-10-17T10:09:00+00:00"}))
    (proj / "reviews").mkdir()
    (proj / "reviews" / "faces.pending").write_text("x")
    (base / "library" / "characters" / f"{bible.characters[0].id}_x").mkdir(parents=True)
    (base / "library" / "characters" / f"{bible.characters[0].id}_x" / "01_front.png").write_bytes(png())
    (base / "queue").mkdir()
    (base / "queue" / "stories.csv").write_text(csv_text([{
        "slug": slug, "story_uri": str(base / "stories" / "demo.md"), "budget_usd": "40", "gates": "ask",
        "publish": "private", "state": "awaiting:faces", "note": "", "updated": "2026-10-17T10:09:00+00:00"}],
        queue.STORY_COLUMNS))
    (base / "queue" / "references.csv").write_text(csv_text([], queue.REFERENCE_COLUMNS))
    return base


@pytest.fixture
def client(root, monkeypatch):
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [
        {"name": "nraiorbit-runner-abc", "created": "2026-10-17T10:00:00", "completed": "", "succeeded": 1, "failed": 0,
         "running": 0, "state": "ok"}])
    monkeypatch.setattr(runner_mod, "logs", lambda *a, **k: ["10:00:01  {'reference': None, 'story': None}"])
    started = []
    monkeypatch.setattr(runner_mod, "run", lambda p, r, j: started.append(j) or "nraiorbit-runner-new")
    app = create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True, project="p",
                                                 region="us-central1", job="nraiorbit-runner"))
    c = TestClient(app, headers={IAP: "owner@example.com"})
    c.started = started
    return c


def rows(root, name=queue.STORY_COLUMNS, file="stories.csv"):
    return list(csv.DictReader(io.StringIO((root / "queue" / file).read_text())))


def test_only_the_allowed_account_gets_in(root):
    app = create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True))
    anon = TestClient(app)
    assert anon.get("/healthz").text == "ok"                                    # the platform's probe
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"            # a browser is sent to sign in
    r = anon.post("/film/demo/retry")
    assert r.status_code == 403 and "No signed-in account" in r.text               # a form post is refused outright
    r = anon.get("/", headers={IAP: "other@example.com"})
    assert r.status_code == 403 and "other@example.com" in r.text and "owner@example.com" in r.text
    r = anon.get("/", headers={IAP: "bad"}, follow_redirects=False)              # an assertion that does not verify
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # the plain e-mail header alone is never enough: without IAP in front anyone could send it
    r = anon.get("/", headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:owner@example.com"},
                 follow_redirects=False)
    assert r.status_code == 303
    assert anon.get("/", headers={IAP: "Owner@Example.com"}).status_code == 200
    local = TestClient(create_app(Store(str(root)), Settings(auth=False)))
    assert local.get("/").status_code == 200                                    # a laptop, no sign-in


def test_policy_pages_are_public(root):
    app = create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True,
                                                 public_url="https://room.example"))
    anon = TestClient(app)
    for path, word in (("/privacy", "Privacy policy"), ("/terms", "Terms of service")):
        r = anon.get(path)
        assert r.status_code == 200 and word in r.text and "owner@example.com" in r.text and "room.example" in r.text


def test_blocked_for_want_of_a_key_is_explained_in_plain_words(client, root):
    import csv as _csv
    import io as _io
    text = (root / "queue" / "stories.csv").read_text()
    rows_ = list(_csv.DictReader(_io.StringIO(text)))
    rows_[0].update(state="blocked:TypeError", note="Could not resolve authentication method. Expected one of api_key…")
    (root / "queue" / "stories.csv").write_text(csv_text(rows_, queue.STORY_COLUMNS))
    assert "no Anthropic API key" in client.get("/").text
    assert "secret ANTHROPIC_API_KEY" in client.get("/film/demo").text


def test_board_shows_the_film_its_gate_and_its_money(client):
    html = client.get("/").text
    assert "demo" in html and STORY_BIBLE["title_en"] in html
    assert "awaiting:faces" in html and "Approve faces" in html
    assert "$1.35" in html and "$40.00" in html and "about $12.50" in html
    assert "nraiorbit-runner-abc" in html and "Run a tick now" in html


def test_film_page_shows_every_stage_and_the_craft(client, root):
    html = client.get("/film/demo").text
    assert "Story bible" in html and STORY_BIBLE["logline"] in html
    first_scene = SCENES[list(SCENES)[0]]
    line = next(b for b in first_scene if b["kind"] == "dialogue")
    assert line["text_te"] in html and line["text_en"] in html                 # dialogue with its subtitle
    assert "First frames" in html and "/file/demo/06_keyframes/" in html         # the keyframe gallery
    assert "/file/library/characters/" in html                                   # the identity image on the cast card
    assert "1 of" in html and "filmed" in html                                   # render progress from inbox/video
    assert "Approve the first frames" in html and "Approve and continue" in html     # plain words, and its step
    assert "Step 7 · First frames" in html and 'class="stage ' not in html             # no tile strip up top
    assert "10:00:01" in html                                                    # the runner log
    # the first frame is served from the local store
    shot = next((root / "demo" / "06_keyframes").iterdir()).name
    assert client.get(f"/file/demo/06_keyframes/{shot}/take_1.png").status_code == 200
    assert client.get("/file/../../etc/passwd").status_code in (404, 400)
    assert client.get("/film/nope").status_code == 404


def test_approve_retry_and_row_edits_write_only_the_sheet(client, root):
    r = client.post("/film/demo/approve", data={"gate": "faces"}, follow_redirects=False)
    assert r.status_code == 303 and "approved" in r.headers["location"] and "tick" in r.headers["location"]
    assert client.started == ["nraiorbit-runner"]                              # the next step starts now, not in 15 min
    row = rows(root)[0]
    assert row["state"] == "approved" and "owner@example.com" in row["note"]
    r = client.post("/film/demo/approve", data={"gate": "faces"}, follow_redirects=False)
    assert r.status_code == 303 and "already" in r.headers["location"] and client.started == ["nraiorbit-runner"]
    html = client.get("/film/demo").text                                        # pressed twice: one tick, no button
    assert 'name="gate"' not in html and 'class="badge awaiting">needs you' not in html
    assert "Approved. The runner takes it up" in html
    assert client.post("/film/demo/approve", data={"gate": "bogus"}).status_code == 400
    client.post("/film/demo/row", data={"budget": "75", "publish": "unlisted", "gates": ""})
    row = rows(root)[0]
    assert row["budget_usd"] == "75" and row["publish"] == "unlisted" and row["gates"] == "ask"
    assert client.post("/film/demo/row", data={"budget": "-5"}).status_code == 400
    from urllib.parse import unquote
    r = client.post("/film/demo/retry", follow_redirects=False)                 # approved: a retry would lose it
    assert "already on it (approved)" in unquote(r.headers["location"]) and rows(root)[0]["state"] == "approved"
    assert client.started == ["nraiorbit-runner"]
    rs = rows(root)
    rs[0].update(state="blocked:FFmpegError", note="ffmpeg failed (254)")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    r = client.post("/film/demo/retry", follow_redirects=False)                 # blocked: run again, now
    assert rows(root)[0]["state"] == "retry" and "tick started (nraiorbit-runner-new)" in unquote(r.headers["location"])
    assert client.started == ["nraiorbit-runner", "nraiorbit-runner"]
    assert not (root / "demo" / "reviews" / "faces.approved").exists()          # the runner does the file work
    r = client.post("/tick", data={"next": "/film/demo"}, follow_redirects=False)
    assert r.status_code == 303 and client.started == ["nraiorbit-runner"] * 3


def test_a_cut_from_before_the_shots_is_stale_and_a_film_without_providers_is_an_animatic(client, root):
    """Steps are read in order: a movie.mp4 that exists while the first frames are still open is from an earlier run
    and shows as stale, never done. A film whose project names no providers runs on the mock ones; the page says
    so, skips the first-frames and cost steps, and the board badges it."""
    proj = root / "demo"
    (proj / "reviews" / "faces.pending").unlink()
    rs = rows(root)
    rs[0].update(state="producing", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (proj / "11_master").mkdir()
    (proj / "11_master" / "movie.mp4").write_bytes(b"")
    html = client.get("/film/demo").text
    assert 'class="badge stale">stale' in html and "from an earlier run; redone after first frames" in html
    assert "Animatic" not in html and "2026-10-17 06:00 EDT" in html         # the runner's 10:00 UTC, on the studio's clock
    (proj / "project" / "pipeline.yaml").write_text(yaml.safe_dump({"film": {"runtime_minutes": 3}, "budget": {"max_usd": 40}}))
    html = client.get("/film/demo").text
    assert "Renders on mocks: picture, voices, music · lips: none" in html and "names no providers" in html
    assert "no first frames: mock video" in html and "nothing to pay: mock providers" in html
    assert 'class="badge skipped">skipped' in html and "film (animatic)" in html and 'class="badge stale">' not in html
    assert 'class="badge held" title="Renders on mocks' in client.get("/").text
    # a delivered, uploaded film whose QA flagged something or never ran: QA is advisory, nothing after it is stale
    (proj / "project" / "pipeline.yaml").write_text(yaml.safe_dump({"film": {"runtime_minutes": 3}, "budget": {"max_usd": 40},
                                                                     "providers": {"video": "veo", "music": "lyria"}}))
    (proj / "11_master" / "deliver").mkdir()
    (proj / "11_master" / "deliver" / "delivery.json").write_text(json.dumps([{"name": "demo_720p.mp4", "mb": 9.0}]))
    rep = json.loads((proj / "reports" / "produce.json").read_text())
    rep.update(state="done", steps={"plan": "done", "cost": "approved", "faces": "approved", "render": "done",
                                    "qa": "skipped: no speaking shots", "publish": {"film": {"id": "vid1"}}})
    (proj / "reports" / "produce.json").write_text(json.dumps(rep))
    for sh in (proj / "04_shots").glob("*.json"):                       # every shot filmed: render and faces done
        for s in json.loads(sh.read_text())["shots"]:
            (proj / "inbox" / "video" / f"{s['shot_id']}.mp4").write_bytes(b"")
    rs = rows(root)
    rs[0]["state"] = "done"
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert 'class="badge stale">' not in html and html.count('class="badge done">done') >= 10
    (proj / "reports" / "qa").mkdir()
    (proj / "reports" / "qa" / "film.json").write_text(json.dumps({"ok": False}))
    html = client.get("/film/demo").text
    assert 'class="badge partial">partial' in html and 'class="badge stale">' not in html


def test_a_run_under_way_offers_no_button_and_a_stale_click_changes_nothing(client, root, monkeypatch):
    """While a tick renders, the bucket still holds the previous stop's report and marker (awaiting:faces,
    faces.pending). The page must not offer that gate again, the board neither, and a click from a stale page must
    not turn the runner's claim back into an approval: that would let the next scheduled tick pay for the step twice.
    A re-run is refused too while the run is under way; once nothing runs, the claim is a run that stopped, the
    page says so, and a re-run is allowed."""
    from urllib.parse import unquote
    rs = rows(root)
    rs[0].update(state="producing", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-busy")])
    html = client.get("/film/demo").text
    assert 'name="gate"' not in html and "Approve faces" not in html and "The runner is on it" in html
    assert "Approve faces" not in client.get("/").text
    r = client.post("/film/demo/approve", data={"gate": "faces"}, follow_redirects=False)
    assert r.status_code == 303 and "nothing waits" in r.headers["location"].replace("%20", " ")
    r = client.post("/film/demo/retry", follow_redirects=False)
    assert "already on it (producing)" in unquote(r.headers["location"])
    assert rows(root)[0]["state"] == "producing" and client.started == []
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-old", state="ok")])
    html = client.get("/film/demo").text                                        # the claim is held by no one now
    assert "The last run stopped without finishing" in html and "Re-run it now" in html
    assert "The runner is on it" not in html
    r = client.post("/film/demo/retry", follow_redirects=False)
    assert rows(root)[0]["state"] == "retry" and client.started == ["nraiorbit-runner"]


def test_a_long_render_beyond_the_listing_window_is_never_offered_a_rerun(client, root, monkeypatch):
    """A render that outlasts six scheduled ticks is the seventh-newest execution or older. "Not among the newest
    few" must never read as "nothing running": a re-run then would drop the claim and pay for the same render
    twice. The film's own record names its execution; only an execution the listing shows finished frees the
    claim, and the listing looks far enough back to find it."""
    from urllib.parse import unquote
    rs = rows(root)
    rs[0].update(state="producing", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reviews" / "faces.pending").unlink()
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps(
        {**PROGRESS, "execution": "nraiorbit-runner-long", "phase": "render", "updated": now_iso(30)}))
    listed = [running_exec(f"nraiorbit-runner-quick{i}", state="ok") for i in range(6)]   # the quick ticks since
    limits = []

    def listing(p, r, j, limit=6):
        limits.append(limit)
        return listed[:limit]
    monkeypatch.setattr(runner_mod, "executions", listing)
    html = client.get("/film/demo").text
    assert "The runner is on it" in html and "Re-run it now" not in html and "stopped without finishing" not in html
    r = client.post("/film/demo/retry", follow_redirects=False)
    assert "already on it (producing)" in unquote(r.headers["location"])
    assert rows(root)[0]["state"] == "producing" and client.started == []
    assert limits and min(limits) >= 24                 # six hours of ticks at one every 15 minutes, and then some
    listed.append(running_exec("nraiorbit-runner-long", state="failed"))   # the listing reaches it: it is over
    html = client.get("/film/demo").text
    assert "stopped without finishing" in html and "Re-run it now" in html
    r = client.post("/film/demo/retry", follow_redirects=False)
    assert rows(root)[0]["state"] == "retry" and client.started == ["nraiorbit-runner"]


def test_no_rerun_over_a_claim_when_cloud_run_cannot_be_asked_or_there_is_no_runner(client, root, monkeypatch):
    from urllib.parse import unquote
    rs = rows(root)
    rs[0].update(state="producing", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))

    def unreachable(*a, **k):
        raise PermissionError("no run.viewer role")
    monkeypatch.setattr(runner_mod, "executions", unreachable)
    html = client.get("/film/demo").text
    assert "Re-run it now" not in html and "stopped without finishing" not in html
    r = client.post("/film/demo/retry", follow_redirects=False)
    assert "already on it (producing)" in unquote(r.headers["location"]) and client.started == []
    # a local control room has no runner to ask: a claim is a claim
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [])
    local = TestClient(create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True)),
                       headers={IAP: "owner@example.com"})
    assert "Re-run it now" not in local.get("/film/demo").text
    r = local.post("/film/demo/retry", follow_redirects=False)
    assert "already on it (producing)" in unquote(r.headers["location"]) and rows(root)[0]["state"] == "producing"


def test_a_claim_renewed_while_the_page_decides_is_kept(client, root, monkeypatch):
    """The narrow race: a crashed run's claim has just expired and the scheduled tick claims the row afresh in the
    seconds the route spends listing executions. The click must not turn that new claim into a retry."""
    from urllib.parse import unquote
    rs = rows(root)
    rs[0].update(state="producing", updated="2026-10-19T10:00:00+00:00")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reviews" / "faces.pending").unlink()

    def listing(*a, **k):                                 # while the page lists executions, a tick claims the row
        fresh = rows(root)
        fresh[0].update(state="producing", updated=queue.now())
        (root / "queue" / "stories.csv").write_text(csv_text(fresh, queue.STORY_COLUMNS))
        return [running_exec("nraiorbit-runner-old", state="ok")]
    monkeypatch.setattr(runner_mod, "executions", listing)
    r = client.post("/film/demo/retry", follow_redirects=False)
    assert "already on it (producing)" in unquote(r.headers["location"])
    assert rows(root)[0]["state"] == "producing" and client.started == []
    assert len(client.get("/").text.split('<tr><td class="mono">')) <= 8   # the board lists a handful, not forty


def test_the_pipeline_flow_shows_the_gates_and_the_step_under_way(client, root, monkeypatch):
    """The film page's pipeline: twelve steps joined by arrows; the step that waits carries its approve button (the
    cost gate shows the estimate, the YouTube gate offers view, download and approve), and with nothing pending the
    first unfinished step is marked as executing while a tick runs."""
    proj = root / "demo"

    def set_state(state, steps):
        rs = rows(root)
        rs[0]["state"] = state
        (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
        rep = json.loads((proj / "reports" / "produce.json").read_text())
        rep["state"] = state
        rep["steps"].update(steps)
        (proj / "reports" / "produce.json").write_text(json.dumps(rep))

    (proj / "reviews" / "faces.pending").unlink()
    (proj / "reviews" / "cost.pending").write_text('{"total_usd": 12.5}')
    set_state("awaiting:cost", {"cost": "awaiting:cost"})
    html = client.get("/film/demo").text
    assert 'name="gate" value="cost"' in html and "Approve $12.50" in html and "needs you" in html
    assert html.count('class="arrow"') == 11
    (proj / "reviews" / "cost.pending").unlink()
    (proj / "reviews" / "publish.pending").write_text("x")
    (proj / "11_master" / "deliver").mkdir(parents=True)
    (proj / "11_master" / "movie.mp4").write_bytes(b"")
    (proj / "11_master" / "trailer.mp4").write_bytes(b"")
    (proj / "11_master" / "deliver" / "delivery.json").write_text(
        json.dumps([{"name": "demo_720p.mp4", "mb": 12.0, "url": None}]))
    set_state("awaiting:publish", {"cost": "approved", "render": "done", "publish": "awaiting:publish"})
    html = client.get("/film/demo").text
    assert "Approve YouTube · private" in html and 'name="gate" value="publish"' in html
    assert "View the film" in html and "/file/demo/11_master/movie.mp4" in html and "Download" in html
    (proj / "reviews" / "publish.pending").unlink()
    set_state("producing", {"publish": "none"})
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [
        {"name": "nraiorbit-runner-xyz", "created": "2026-10-17T10:00:00", "completed": "", "succeeded": 0, "failed": 0,
         "running": 1, "state": "running"}])
    html = client.get("/film/demo").text
    assert "running now" in html and 'class="badge next live">running</span>' in html
    from nraiorbit.ui import films
    step = films.detail(Store(str(root)), "demo", rows(root)[0])["next_stage"]   # no progress record: the old runner
    assert step and 'class="badge executing live"' in card(html, "money" if step == "cost" else step)
    assert "todo</span>" not in card(html, "money" if step == "cost" else step)


def test_new_story_writes_the_story_the_overrides_and_the_row(client, root):
    story = "గోదావరి ఒడ్డున ఒక పడవ నడిపే యువకుడు వరదలో ఊరిని కాపాడతాడు. " * 2
    r = client.post("/new", data={"slug": "rain-ferry", "story": story, "budget": "120", "runtime": "5", "gates": "ask",
                                  "publish": "private", "start": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/film/rain-ferry")
    assert (root / "stories" / "rain-ferry.md").read_text(encoding="utf-8").strip() == story.strip()
    assert yaml.safe_load((root / "stories" / "rain-ferry.overrides.yaml").read_text()) == {"film": {"runtime_minutes": 5}}
    new = rows(root)[-1]
    assert new["slug"] == "rain-ferry" and new["state"] == "new" and new["budget_usd"] == "120"
    assert new["story_uri"].endswith("stories/rain-ferry.md") and client.started == ["nraiorbit-runner"]
    assert client.get("/film/rain-ferry").status_code == 200                    # a queued film with no plan yet
    # any name works: it is turned into the folder name; a taken name gets -2; an empty one comes from the story
    r = client.post("/new", data={"slug": "Rain Ferry!", "story": story, "budget": "10"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/film/rain-ferry-2")
    r = client.post("/new", data={"slug": "demo", "story": story, "budget": "10"}, follow_redirects=False)
    assert r.headers["location"].startswith("/film/demo-2")                     # demo is a film already
    english = "The ferryman of Godavari saves the village in a flood. " * 3
    r = client.post("/new", data={"slug": "", "story": english, "budget": "10"}, follow_redirects=False)
    assert r.headers["location"].startswith("/film/the-ferryman-of-godavari-saves-the")
    r = client.post("/new", data={"slug": "గోదావరి సింహం", "story": story, "budget": "10"}, follow_redirects=False)
    assert r.headers["location"].startswith("/film/film-20")                    # nothing Latin to use: the date
    # real mistakes come back as the form with the message and the text kept, never a bare error page
    r = client.post("/new", data={"slug": "ok", "story": "short", "budget": "10"})
    assert r.status_code == 400 and "too short" in r.text and 'name="story"' in r.text
    r = client.post("/new", data={"slug": "ok", "story": story, "budget": "0", "publish": "unlisted"})
    assert r.status_code == 400 and "above zero" in r.text and story[:20] in r.text and 'value="unlisted" selected' in r.text
    assert app_mod.slugify("Rain Ferry!") == "rain-ferry" and app_mod.slugify("ఆ") == "" and app_mod.slugify("Café-7") == "cafe-7"
    # the cap must be a real number: nan and inf would switch the hard stop off; a huge runtime is a form error
    for bad_cap in ("nan", "inf", "1e999", "-inf"):
        r = client.post("/new", data={"slug": "capless", "story": story, "budget": bad_cap})
        assert r.status_code == 400 and "real number above zero" in r.text, bad_cap
    assert not any(row["slug"] == "capless" for row in rows(root))
    r = client.post("/new", data={"slug": "longrun", "story": story, "budget": "10", "runtime": "1e999"})
    assert r.status_code == 400 and "Runtime must be a number" in r.text
    assert client.post("/film/demo/row", data={"budget": "inf"}).status_code == 400
    # cents survive the sheet; the preview on the page and the server agree on punctuation and accents
    client.post("/film/demo/row", data={"budget": "99999.99"})
    assert rows(root)[0]["budget_usd"] == "99999.99"
    assert app_mod.money_text(150) == "150" and app_mod.money_text(1234567.5) == "1234567.5"
    assert app_mod.slugify("Ferryman\u2019s Daughter") == "ferryman-s-daughter"
    assert app_mod.slugify("Rain\u2014Ferry") == "rain-ferry" and app_mod.slugify("Stra\u00dfe") == "stra-e"
    # a name that is a bucket prefix or a route of this app never becomes a film folder
    r = client.post("/new", data={"slug": "queue", "story": story, "budget": "10"}, follow_redirects=False)
    assert r.headers["location"].startswith("/film/queue-2")
    assert app_mod.pick_slug("library", story, lambda s: False) == "library-2"
    assert app_mod.pick_slug("x" * 60, story, lambda s: s == "x" * 41) == "x" * 39 + "-2"


def test_reference_queue(client, root):
    assert "No references yet" in client.get("/references").text
    r = client.post("/references", data={"url": "https://www.youtube.com/watch?v=abc123xyz", "title": "Ref",
                                         "learn": "score", "minutes": "140"}, follow_redirects=False)
    assert r.status_code == 303
    ref = rows(root, queue.REFERENCE_COLUMNS, "references.csv")[0]
    assert ref["state"] == "new" and ref["minutes"] == "140" and ref["learn"] == "score"
    assert client.post("/references", data={"url": "https://example.com/x"}).status_code == 400
    (root / "queue" / "references.csv").write_text(
        csv_text([{**ref, "state": "blocked:LearnCostError"}], queue.REFERENCE_COLUMNS))
    client.post("/references/retry", data={"url": ref["url"]})
    assert rows(root, queue.REFERENCE_COLUMNS, "references.csv")[0]["state"] == "retry"
    # times on the studio's clock: a Z-suffixed stamp converts, a malformed one is shown as it is, escaped
    (root / "queue" / "references.csv").write_text(csv_text([
        {**ref, "updated": "2026-10-17T10:00:00Z"}, {**ref, "url": "https://youtu.be/x2", "updated": "<yesterday>"}],
        queue.REFERENCE_COLUMNS))
    page = client.get("/references").text
    assert "2026-10-17 06:00 EDT" in page and "&lt;yesterday&gt;" in page


def test_sign_in_with_google_sets_a_session_and_keeps_strangers_out(root, monkeypatch):
    tokens = {"good": {"iss": "https://accounts.google.com", "email": "Owner@Example.com", "email_verified": True},
              "stranger": {"iss": "https://accounts.google.com", "email": "other@example.com", "email_verified": True}}
    seen = []
    monkeypatch.setattr(app_mod, "verify_google_token", lambda tok, cid: seen.append(cid) or tokens.get(tok))
    app = create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True,
                                                 google_client_id="123-abc.apps.googleusercontent.com",
                                                 session_secret="s3cret"))
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"              # a browser is sent to sign in
    page = c.get("/login").text
    assert "123-abc.apps.googleusercontent.com" in page and "/auth/callback" in page
    # the Google button posts the credential with its csrf token (cookie + field)
    c.cookies.set("g_csrf_token", "xyz")
    r = c.post("/auth/callback", data={"credential": "good", "g_csrf_token": "xyz"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and seen == ["123-abc.apps.googleusercontent.com"]
    assert c.cookies.get(app_mod.SESSION_COOKIE)
    assert c.get("/").status_code == 200 and "owner@example.com" in c.get("/").text
    assert c.post("/film/demo/retry", follow_redirects=False).status_code == 303   # actions work on the session
    c.get("/logout")
    assert c.get("/", follow_redirects=False).status_code == 303
    # a stranger with a valid Google account is refused by name; a missing csrf token is refused; a bad token too
    c.cookies.set("g_csrf_token", "xyz")
    r = c.post("/auth/callback", data={"credential": "stranger", "g_csrf_token": "xyz"})
    assert r.status_code == 403 and "other@example.com" in r.text
    assert c.post("/auth/callback", data={"credential": "good", "g_csrf_token": "nope"}).status_code == 400
    assert c.post("/auth/callback", data={"credential": "forged", "g_csrf_token": "xyz"}).status_code == 401
    # a tampered cookie is worthless
    c.cookies.set(app_mod.SESSION_COOKIE, "owner@example.com|9999999999|deadbeef")
    assert c.get("/", follow_redirects=False).status_code == 303
    assert app_mod.read_session("s3cret", app_mod.sign_session("s3cret", "a@b.c", 1)) == "a@b.c"
    assert app_mod.read_session("other", app_mod.sign_session("s3cret", "a@b.c", 1)) is None


def test_passphrase_sign_in_is_throttled_and_mints_the_same_session(root, monkeypatch):
    monkeypatch.setattr(app_mod.time, "sleep", lambda *_: None, raising=False) if hasattr(app_mod, "time") else None
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_: None)
    app = create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True,
                                                 passphrase="correct horse battery staple", session_secret="s3cret"))
    c = TestClient(app)
    assert 'action="/auth/passphrase"' in c.get("/login").text
    r = c.post("/auth/passphrase", data={"passphrase": "wrong"}, follow_redirects=False)
    assert r.status_code == 303 and "wrong" in r.headers["location"] and not c.cookies.get(app_mod.SESSION_COOKIE)
    r = c.post("/auth/passphrase", data={"passphrase": " correct horse battery staple "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and c.cookies.get(app_mod.SESSION_COOKIE)
    page = c.get("/").text
    assert "owner@example.com (passphrase)" in page
    assert c.post("/film/demo/retry", follow_redirects=False).status_code == 303
    c.get("/logout")
    for _ in range(8):
        c.post("/auth/passphrase", data={"passphrase": "nope"}, follow_redirects=False)
    assert c.post("/auth/passphrase", data={"passphrase": "correct horse battery staple"}).status_code == 429
    # without a passphrase configured the route does not exist
    plain = TestClient(create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True)))
    assert plain.post("/auth/passphrase", data={"passphrase": "x"}).status_code == 404



def test_connect_youtube_stores_the_channel_token_and_shows_the_channel(root, monkeypatch):
    import json
    calls = {}

    def exchange(code, cid, sec, uri):
        calls["exchange"] = (code, cid, uri)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/youtube.upload"}

    def channel(tj):
        calls["token"] = json.loads(tj)
        return {"id": "UC123", "title": "NRAIORBIT Cinematic Universe"}

    def store(tj):
        calls["stored"] = tj
        return True
    monkeypatch.setattr(app_mod, "exchange_code", exchange)
    monkeypatch.setattr(app_mod, "channel_info", channel)
    monkeypatch.setattr(app_mod, "store_token", store)
    app = create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True,
                                                 google_client_id="cid.apps.googleusercontent.com",
                                                 google_client_secret="shh", session_secret="s3cret"))
    c = TestClient(app, headers={IAP: "owner@example.com"})
    home = c.get("/").text
    assert "Connect YouTube" in home and "No channel connected" in home
    from urllib.parse import parse_qs, urlparse
    # behind a TLS-terminating proxy the redirect must still say https, and a configured public URL wins outright
    r2 = c.get("/youtube/connect", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "room.example"},
               follow_redirects=False)
    assert parse_qs(urlparse(r2.headers["location"]).query)["redirect_uri"][0] == "https://room.example/youtube/callback"
    pinned = create_app(Store(str(root)), Settings(allowed_emails={"owner@example.com"}, auth=True,
                                                    google_client_id="cid", google_client_secret="shh",
                                                    session_secret="s3cret", public_url="https://pinned.example/"))
    r3 = TestClient(pinned, headers={IAP: "owner@example.com"}).get("/youtube/connect", follow_redirects=False)
    assert parse_qs(urlparse(r3.headers["location"]).query)["redirect_uri"][0] == "https://pinned.example/youtube/callback"
    assert 'data-login_uri="https://pinned.example/auth/callback"' in TestClient(pinned).get("/login").text
    r = c.get("/youtube/connect", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in r.headers["location"] and "youtube.upload" in r.headers["location"]
    q = parse_qs(urlparse(r.headers["location"]).query)
    state = q["state"][0]
    assert q["redirect_uri"][0].endswith("/youtube/callback")
    # a callback with another state is refused; the right one stores the token and the channel record
    assert c.get("/youtube/callback", params={"code": "c0de", "state": "other"}).status_code == 400
    r = c.get("/youtube/callback", params={"code": "c0de", "state": state}, follow_redirects=False)
    assert r.status_code == 303 and "NRAIORBIT" in r.headers["location"]
    assert calls["exchange"][0] == "c0de" and calls["exchange"][1] == "cid.apps.googleusercontent.com"
    assert calls["token"]["refresh_token"] == "rt" and calls["token"]["client_secret"] == "shh"
    assert calls["token"]["type"] == "authorized_user" and json.loads(calls["stored"])["refresh_token"] == "rt"
    rec = json.loads((root / "settings" / "youtube.json").read_text())
    assert rec["channel"] == "NRAIORBIT Cinematic Universe" and rec["connected_by"] == "owner@example.com"
    home = c.get("/").text
    assert "Connected to <b>NRAIORBIT Cinematic Universe</b>" in home
    assert "Channel: NRAIORBIT Cinematic Universe" in c.get("/film/demo").text
    # Google declining, and no refresh token, both come back as a message, not a crash
    r = c.get("/youtube/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert r.status_code == 303 and "access_denied" in r.headers["location"]
    monkeypatch.setattr(app_mod, "exchange_code", lambda *a: {"access_token": "at", "expires_in": 10})
    r = c.get("/youtube/connect", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get("/youtube/callback", params={"code": "x", "state": state}, follow_redirects=False)
    from urllib.parse import unquote
    assert "no refresh token" in unquote(r.headers["location"])
    # without the client secret the button is not offered
    (root / "settings" / "youtube.json").unlink()
    plain = TestClient(create_app(Store(str(root)), Settings(auth=False)))
    assert "Connecting needs the Google OAuth client" in plain.get("/").text


def test_publish_paths_find_the_token_in_the_environment_or_secret_manager(tmp_path, monkeypatch):
    pytest.importorskip("google.oauth2.credentials")
    from nraiorbit.ops import secrets
    from nraiorbit.publish import youtube
    token = tmp_path / ".youtube_token.json"
    monkeypatch.delenv("YOUTUBE_TOKEN_JSON", raising=False)
    monkeypatch.setattr(secrets, "read", lambda name, project_id=None: None)
    assert youtube.stored_token() is None and not youtube.token_available(token)
    seed = json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                       "scopes": youtube.SCOPES, "type": "authorized_user", "expiry": "2099-01-01T00:00:00Z"})
    monkeypatch.setattr(secrets, "read", lambda name, project_id=None: seed if name == "YOUTUBE_TOKEN_JSON" else None)
    assert youtube.token_available(token)
    creds = youtube.credentials(tmp_path / "client_secret.json", token, interactive=False)
    assert creds.valid and token.exists()                      # seeded from Secret Manager, no browser
    monkeypatch.setenv("YOUTUBE_TOKEN_JSON", seed.replace('"t"', '"env"'))
    assert json.loads(youtube.stored_token())["token"] == "env"   # the environment wins over the secret



def card(html: str, key: str) -> str:
    """One stage's card on the film page, from its anchor to the next card."""
    i = html.index(f'class="card" id="{key}"') if f'class="card" id="{key}"' in html else html.index(f'id="{key}"')
    j = html.find('<div class="card', i + 1)
    return html[i: j if j != -1 else len(html)]

def running_exec(name, created="2026-10-19T06:46:02+00:00", state="running"):
    return {"name": name, "created": created, "completed": "" if state == "running" else "2026-10-19T07:30:00+00:00",
            "succeeded": int(state == "ok"), "failed": int(state == "failed"), "running": int(state == "running"),
            "state": state, "url": f"https://console.cloud.google.com/run/jobs/executions/details/r/{name}?project=p"}


def producing(root, record=None):
    """The film claimed by a tick (no gate pending), and the progress record the runner keeps while it works."""
    rs = rows(root)
    rs[0].update(state="producing", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reviews" / "faces.pending").unlink()
    if record is not None:
        (root / "demo" / "reports" / "progress.json").write_text(json.dumps(record))


def now_iso(seconds_ago=0):
    import datetime
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return t.isoformat(timespec="seconds")


PROGRESS = {"slug": "demo", "execution": "nraiorbit-runner-mfkkm", "state": "running", "phase": "scenes",
            "phase_started": "2026-10-19T06:48:10+00:00", "started": "2026-10-19T06:46:02+00:00",
            "last_line": "sc010: 3 lines", "lines": 17}


def test_the_runners_progress_record_marks_the_stage_under_way(client, root, monkeypatch):
    """The runner's own word beats the inference from plan files: while its execution runs, the phase in
    reports/progress.json is the stage under way, with when it began and how fresh its last line is. The same record
    once that execution has ended is ignored, so a crash never leaves a stage "executing"."""
    from nraiorbit.ui import films
    producing(root, {**PROGRESS, "updated": now_iso(40)})
    store = Store(str(root))
    d = films.detail(store, "demo", rows(root)[0], {"nraiorbit-runner-mfkkm"})
    assert d["stages"]["scenes"]["status"] == "executing" and d["next_stage"] == "scenes"
    assert d["stages"]["scenes"]["text"].startswith("scenes since 06:48 UTC · last: sc010: 3 lines (")
    assert d["live"]["stage_key"] == "scenes" and d["live"]["since"] == "06:48 UTC" and d["live"]["ago"].endswith("s ago")
    assert d["gates"] == [] and d["gate_for"] == {}
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-mfkkm")])
    html = client.get("/film/demo").text
    assert ("Running <b>Scenes &amp; dialogue</b> · Claude Opus 5.5 · Claude Opus 5.5 (critic) · since 02:48 EDT · "
            "last: sc010: 3 lines (") in html
    assert "execution <span class=\"mono\">nraiorbit-runner-mfkkm</span>" in html
    assert 'class="node executing"' in html and 'class="badge executing live">running</span>' in html
    assert 'Scenes &amp; dialogue<span class="badge executing live"' in card(html, "scenes")
    assert "Now: <b>Writing the scenes and dialogue</b>" in html             # what runs, in plain words
    assert '<noscript><meta http-equiv="refresh" content="20"></noscript>' in html and "sessionStorage" in html
    assert "location.reload()" in html and "pagehide" not in html             # only its own refresh keeps the place
    assert 'name="gate"' not in html                                           # a run under way offers no button
    rs = rows(root)
    rs[0]["state"] = "approved"                                                 # approved, the tick not yet claimed
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert 'name="gate"' not in html and "Approve" not in html and "Running <b>" in html
    # QA under way: its card says running, never todo (the owner watched QA read TODO while it ran)
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "phase": "qa", "updated": now_iso(5)}))
    html = client.get("/film/demo").text
    assert '<span class="badge executing live">running</span>' in card(html, "qa") and "todo</span>" not in card(html, "qa")
    assert "running now · qa" in html
    # the music plan is drawn on the render stage and says so
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "phase": "music_plan"}))
    d = films.detail(store, "demo", rows(root)[0], {"nraiorbit-runner-mfkkm"})
    assert d["next_stage"] == "render" and d["stages"]["render"]["text"].startswith("music plan since")
    # a phase with no stage (the restore, or none yet) is live for the header only; the inference picks the stage
    for phase in ("restore", None):
        (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "phase": phase}))
        d = films.detail(store, "demo", rows(root)[0], {"nraiorbit-runner-mfkkm"})
        assert d["live"]["stage_key"] is None and "executing" not in {s["status"] for s in d["stages"].values()}
    html = client.get("/film/demo").text
    assert "Running since 02:48 EDT · last: sc010" in html
    assert "running now · " in html and "on the next tick" not in html      # a run is on the film: never "next tick"
    # the execution has ended, or the record says it finished: not live, today's inference stands
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps(PROGRESS))
    before = films.detail(store, "demo", rows(root)[0])
    for running, record in (({"nraiorbit-runner-other"}, PROGRESS), ({"nraiorbit-runner-mfkkm"}, {**PROGRESS, "state": "done"})):
        (root / "demo" / "reports" / "progress.json").write_text(json.dumps(record))
        d = films.detail(store, "demo", rows(root)[0], running)
        assert d["live"] is None and d["next_stage"] == before["next_stage"] and d["stages"] == before["stages"]
        assert "executing" not in {s["status"] for s in d["stages"].values()}
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-mfkkm", state="ok")])
    html = client.get("/film/demo").text
    assert "Running <b>" not in html and 'class="node executing"' not in html and 'class="stage executing"' not in html
    assert '<meta http-equiv="refresh" content="45">' in html                 # still producing by the sheet
    (root / "demo" / "reports" / "progress.json").write_text("{not json")       # half-written: the page still renders
    assert client.get("/film/demo").status_code == 200


def test_the_runner_table_says_what_each_execution_is_doing(client, root, monkeypatch):
    """queue/running/<execution>.json is each tick's own word on what it does; Cloud Run says whether it still runs.
    A record whose execution has ended was left behind; two running executions on one film are a duplicate."""
    running = root / "queue" / "running"
    running.mkdir()
    rec = {"started": "2026-10-19T06:46:02+00:00", "updated": now_iso(12), "doing": "story", "slug": "demo",
           "phase": "music_plan", "last_line": "cue 3"}
    (running / "nraiorbit-runner-a.json").write_text(json.dumps({**rec, "execution": "nraiorbit-runner-a"}))
    (running / "nraiorbit-runner-b.json").write_text(json.dumps({"execution": "nraiorbit-runner-b", "doing": "idle"}))
    (running / "nraiorbit-runner-old.json").write_text(json.dumps({**rec, "execution": "nraiorbit-runner-old"}))
    (running / "broken.json").write_text("{")
    assert set(runner_mod.doings(Store(str(root)))) == {"nraiorbit-runner-a", "nraiorbit-runner-b", "nraiorbit-runner-old"}
    execs = [running_exec("nraiorbit-runner-a"), running_exec("nraiorbit-runner-b"),
             running_exec("nraiorbit-runner-old", state="failed")]
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: execs)
    for page in ("/", "/film/demo"):
        html = client.get(page).text
        assert "<th>Doing</th>" in html and '<a href="/film/demo">demo</a> · music plan · tick started 02:46 EDT' in html
        assert "nothing to do" in html and "left behind" in html and "duplicate" not in html
        assert ('<a href="https://console.cloud.google.com/run/jobs/executions/details/r/nraiorbit-runner-a?project=p" '
                'target="_blank" rel="noopener">nraiorbit-runner-a</a>') in html
    assert '<meta http-equiv="refresh" content="45">' in client.get("/").text
    # a second tick on the same film: both rows say so, and the page says what to do
    (running / "nraiorbit-runner-b.json").write_text(json.dumps({**rec, "execution": "nraiorbit-runner-b"}))
    for page in ("/", "/film/demo"):
        html = client.get(page).text
        assert html.count('class="badge blocked">duplicate</span>') == 2
        assert "two executions on the same film; cancel the newer one in the console" in html
    assert app_mod.duplicates(execs, runner_mod.doings(Store(str(root)))) == {"nraiorbit-runner-a", "nraiorbit-runner-b"}
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-a", state="ok")])
    assert '<meta http-equiv="refresh" content="90">' in client.get("/").text


def test_a_tick_is_never_started_over_a_running_one(client, root, monkeypatch):
    from urllib.parse import unquote
    started = client.started
    monkeypatch.setattr(runner_mod, "run", lambda *a: pytest.fail("a second tick was started"))
    # running, or just started and still pending while Cloud Run schedules it
    pending = {**running_exec("nraiorbit-runner-new"), "running": 0, "state": "pending"}
    for busy in (running_exec("nraiorbit-runner-busy"), pending):
        monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [busy])
        r = client.post("/tick", data={"next": "/film/demo"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/film/demo?")
        assert f"a tick is already running ({busy['name']}); a second one would repeat its work" in \
            unquote(r.headers["location"])
    # a pending one that has a completion time was cancelled; and when Cloud Run cannot be asked, a tick still starts
    cancelled = {**pending, "completed": "2026-10-19T06:47:00+00:00"}

    def unreachable(*a, **k):
        raise PermissionError("no run.viewer role")
    monkeypatch.setattr(runner_mod, "run", lambda p, r, j: started.append(j) or "nraiorbit-runner-next")
    for listing in (lambda *a, **k: [cancelled], unreachable):
        monkeypatch.setattr(runner_mod, "executions", listing)
        r = client.post("/tick", data={"next": "/"}, follow_redirects=False)
        assert r.status_code == 303 and "tick started (nraiorbit-runner-next)" in unquote(r.headers["location"])
    assert started == ["nraiorbit-runner", "nraiorbit-runner"]


def test_the_log_header_says_when_the_execution_ran_and_spoke(client, root, monkeypatch):
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-x", state="ok")])
    monkeypatch.setattr(runner_mod, "logs", lambda *a, **k: ["06:46:03  restore", "07:29:58  backup done", ""])
    html = client.get("/film/demo").text
    assert "execution <a href=" in html and "started 2026-10-19 02:46:02 EDT · finished 03:30:00 EDT" in html
    assert "last line 07:29:58 (" in html and " ago)" in html
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-x",
                                                                                created=now_iso(125))])
    assert "running for 2 min" in client.get("/film/demo").text
    # the date of a log line comes from the execution's start, so an old execution reads as old
    assert app_mod.log_time(["23:59:58  a", "00:00:03  b"], "2026-10-17T23:50:00+00:00") == "2026-10-18T00:00:03+00:00"
    assert app_mod.log_time(["10:00:01  x"], "2026-10-17T10:00:00") == "2026-10-17T10:00:01+00:00"
    assert app_mod.log_time(["(log unavailable: Forbidden)"], "2026-10-17T10:00:00") is None
    import datetime
    noon = datetime.datetime(2026, 10, 19, 12, 0, tzinfo=datetime.timezone.utc)
    assert app_mod.log_time(["23:00:00  late"], None, now=noon) == "2026-10-18T23:00:00+00:00"


def test_ago_reads_a_timestamp_as_its_age():
    import datetime

    from nraiorbit.ui import films
    now = datetime.datetime(2026, 10, 19, 7, 5, 46, tzinfo=datetime.timezone.utc)
    assert films.ago("2026-10-19T07:05:06+00:00", now) == "40 s ago"
    assert films.ago("2026-10-19T06:53:00Z", now) == "12 min ago"
    assert films.ago("2026-10-19T04:00:00", now) == "3 h ago"                   # a naive time is UTC
    assert films.ago("2026-10-16T07:00:00+00:00", now) == "3 d ago"
    assert films.ago("2026-10-19T07:06:00+00:00", now) == "0 s ago"             # a clock a little ahead
    for bad in ("", None, "yesterday", 17):
        assert films.ago(bad, now) == ""
    assert films.clock("2026-10-19T08:48:10+01:00") == "07:48 UTC" and films.clock("nope") == ""


def node(html: str, label: str) -> str:
    """One pipeline node on the film page, from its link to the end of its list item."""
    i = html.index(f'title="{label}: details"')
    return html[i: html.index("</li>", i)]


def test_each_stage_names_the_service_or_model_behind_it(client, root):
    """The film's own config decides: one that names no providers renders on mocks, and the page says so in the
    render node and the note; one with the template's providers names Veo, the voices, Lyria and sync.so."""
    from nraiorbit import tools
    proj = root / "demo"
    (proj / "project" / "pipeline.yaml").write_text(yaml.safe_dump(
        {"film": {"runtime_minutes": 3}, "budget": {"max_usd": 30}, "policy": {"gates": "ask", "publish": "private"}}))
    html = client.get("/film/demo").text
    render = node(html, "Picture, voices, music, lips")
    assert 'class="tool mock" title="mock · colour cards (ffmpeg)">picture: mock (no paid call)</span>' in render
    assert "lips: none" in render and "Veo" not in render
    assert "Renders on mocks: picture, voices, music · lips: none." in html and "Claude planning is still billed" in html
    assert 'title="claude-opus-5-5 (story_analyst) · effort high">Claude Opus 5.5</span>' in node(html, "Story bible")
    assert "not used (video: mock)" in node(html, "First frames")
    assert "YouTube Data API v3 (private)" in node(html, "YouTube")
    assert "Tools:" in card(html, "render") and "planned" not in render
    (proj / "project" / "pipeline.yaml").write_text(yaml.safe_dump(tools.template()))
    html = client.get("/film/demo").text
    render = node(html, "Picture, voices, music, lips")
    assert "Veo 3.1 Fast 720p" in render and "sync.so sync-3" in render and "ElevenLabs eleven_v4" in render
    assert "Renders on mocks" not in html and "Gemini image gemini-2.5-flash-image" in node(html, "First frames")
    assert "Claude Opus 5.5 (cue sheet)" in render and "Veo 3.1 Fast 720p" in render   # in the pipeline below
    board = client.get("/").text
    assert "Render: Veo 3.1 Fast 720p · inbox recordings" in board and ">mock</span>" not in board


def test_a_config_that_cannot_be_read_still_renders(client, root):
    """A hand-edited pipeline.yaml that is not YAML, or not a mapping, must not take the page or the board down."""
    for text in ("providers: [veo\n", "just a line of text\n", "- a\n- list\n"):
        (root / "demo" / "project" / "pipeline.yaml").write_text(text)
        r = client.get("/film/demo")
        assert r.status_code == 200 and "Tools:" in r.text
        assert client.get("/").status_code == 200


def test_a_film_made_by_hand_without_a_config_runs_on_the_defaults(client, root):
    """`nraiorbit new --bare` makes a project with no pipeline.yaml and no queue row: no tick ever gives it the
    template, so the page shows the defaults it runs on (mocks), never a plan."""
    (root / "demo" / "project" / "pipeline.yaml").unlink()
    (root / "queue" / "stories.csv").write_text(csv_text([], queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert "Renders on mocks: picture, voices, music · lips: none" in html and "Tools shown are planned" not in html
    assert "names no providers" in html and "Add the providers to its pipeline.yaml" in html
    assert "Queue it again" not in html and 'class="tool planned"' not in html
    board = client.get("/").text
    assert "Render: picture: mock (no paid call) · voices: mock (no paid call)" in board and ">planned</span>" not in board


def test_used_shows_what_the_reports_say_was_called(client, root):
    proj = root / "demo"
    (proj / "reports" / "llm_usage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"agent": "story_analyst", "schema": "StoryBible", "model": "claude-opus-5-5"},
        {"agent": "director", "schema": "ShotList", "model": "claude-sonnet-4-6"}]))
    html = client.get("/film/demo").text
    assert "Used: <span>Claude Opus 5.5 x1</span>" in card(html, "story")
    assert 'style="color:var(--warn)"' in card(html, "shots") and "Claude Sonnet 4.6 x1" in card(html, "shots")
    assert "veo x1 ($1.20)" in card(html, "render") and "gemini_image x1 ($0.15)" in card(html, "faces")


def test_a_queued_film_without_its_config_shows_the_template_as_planned(client, root):
    """Before its first tick a film has no project/pipeline.yaml: the page shows what the runner will make of it
    (the template, the story's overrides, the row), marked planned. The board does the same without the overrides."""
    (root / "stories").mkdir()
    (root / "stories" / "rain.md").write_text("Rain on the river.")
    (root / "stories" / "rain.overrides.yaml").write_text(yaml.safe_dump({"voice": {"inbox_fallback": "mock"}}))
    rs = rows(root)
    rs.append({"slug": "rain", "story_uri": str(root / "stories" / "rain.md"), "budget_usd": "20", "gates": "auto",
               "publish": "none", "state": "new", "note": "", "updated": queue.now()})
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/rain").text
    assert "planned (the template&#39;s choices; the first tick writes the film&#39;s own config)" in html
    assert 'class="tool planned"' in html and "Tools shown are planned" in html
    assert 'class="tag bl"' not in html                       # the hero frame does not shout the planned note
    render = node(html, "Picture, voices, music, lips")
    assert "Veo 3.1 Fast 720p" in render and "first frames during render" in render     # the row's gates: auto
    assert "Voices without an inbox recording fall back to mock." in html                 # the story's overrides
    assert "YouTube Data API v3" not in node(html, "YouTube")
    board = client.get("/").text
    assert ">planned</span>" in board and "Render: <i>Gemini image" in board


def test_the_hero_names_the_tools_of_the_phase_under_way(client, root, monkeypatch):
    from nraiorbit.ui import films
    producing(root, {**PROGRESS, "phase": "render", "updated": now_iso(10)})
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-mfkkm")])
    html = client.get("/film/demo").text
    assert ("Running <b>Picture, voices, music, lips</b> · Veo 3.1 Fast 720p · voices: mock (no paid call) · "
            "Lyria 3 lyria-3-pro-preview · lips: none · since 02:48 EDT") in html
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "phase": "music_plan"}))
    assert "Running <b>Picture, voices, music, lips</b> · Claude Opus 5.5 (cue sheet) · since" in \
        client.get("/film/demo").text
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "phase": "trailer"}))
    assert "Running <b>Film &amp; trailer</b> · Lyria 3 lyria-3-pro-preview · since" in client.get("/film/demo").text
    for phase in (None, "backup"):                         # no stage: the header only, and no tools line
        (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "phase": phase}))
        r = client.get("/film/demo")
        assert r.status_code == 200 and ("Running since 02:48 EDT" in r.text if phase is None
                                         else "Running <b>backup</b> since 02:48 EDT" in r.text)
        d = films.detail(Store(str(root)), "demo", rows(root)[0], {"nraiorbit-runner-mfkkm"})
        assert d["live"] and "tools_line" not in d["live"]


def flow_node(html: str, key: str) -> str:
    """One step's node in the pipeline flow: from its anchor to the next node."""
    start = html.index('<ol class="flow">')                 # past the mini strip, which uses the same anchors
    i = html.index(f'href="#{"money" if key == "cost" else key}" title=', start)
    i = html.rindex("<li class=\"node", start, i)
    j = html.find("<li class=\"node", i + 1)
    return html[i: j if j != -1 else len(html)]


def test_a_blocked_film_names_the_failed_step_on_its_card_and_reruns_from_there(client, root, monkeypatch):
    from urllib.parse import unquote
    rs = rows(root)
    rs[0].update(state="blocked:FFmpegError", note="ffmpeg failed (254): ffmpeg -hide_banner -i 10_edit/film_dialogue.mp4")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reviews" / "faces.pending").unlink()
    # the runner's progress record ended on the trailer: that is the step that failed, whatever the cut looks like
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "state": "blocked", "phase": "trailer"}))
    html = client.get("/film/demo").text
    node = flow_node(html, "assemble")
    assert 'class="badge blocked">blocked' in node and "ffmpeg failed (254)" in node
    assert "Failed here: FFmpegError." in node and "Fix is in place, re-run from here" in node
    assert "keeps the finished steps, redoes this one and the rest" in node
    assert "Fix is in place, re-run from here" not in flow_node(html, "render")   # one card carries the failure
    # without a progress record the first open step is the one named
    (root / "demo" / "reports" / "progress.json").unlink()
    html = client.get("/film/demo").text
    assert "Fix is in place, re-run from here" in flow_node(html, "faces")
    r = client.post("/film/demo/retry", follow_redirects=False)
    assert rows(root)[0]["state"] == "retry" and client.started == ["nraiorbit-runner"]
    assert "the pipeline shows the step under way" in unquote(r.headers["location"])
    html = client.get("/film/demo").text                                        # queued again: no second re-run button
    assert "re-run from here" not in html and "Re-run the film" not in html


def test_the_youtube_step_offers_a_rerun_once_the_film_is_settled(client, root):
    rs = rows(root)
    (root / "demo" / "reviews" / "faces.pending").unlink()
    (root / "demo" / "11_master").mkdir()
    (root / "demo" / "11_master" / "movie.mp4").write_bytes(b"film")
    for state in ("awaiting:publish", "done"):
        rs[0].update(state=state)
        (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
        node = flow_node(client.get("/film/demo").text, "publish")
        assert "Re-run the film" in node and "finished ones are kept, missing or failed ones are redone" in node
        assert ("Approve YouTube" in node) == (state == "awaiting:publish")
        assert "View the film" in node
    rs[0].update(state="producing")                                             # a run under way: never a second one
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    assert "Re-run the film" not in client.get("/film/demo").text


def test_an_animatic_says_so_on_the_film_and_youtube_steps(client, root):
    cfg = yaml.safe_load((root / "demo" / "project" / "pipeline.yaml").read_text())
    cfg.pop("providers")                                                        # no providers: the mock ones
    (root / "demo" / "project" / "pipeline.yaml").write_text(yaml.safe_dump(cfg))
    html = client.get("/film/demo").text
    assert "animatic: placeholder picture and mock voices; approving uploads it as it is" in flow_node(html, "publish")
    assert "animatic: placeholder picture and mock voices" in flow_node(html, "assemble")
    assert "animatic:" not in flow_node(html, "cost")
    assert films.cost_lines({"video_usd": 1.0}, {}, [], [], [], []) == [{"name": "video", "usd": 1.0, "why": ""}]


def test_the_cost_step_carries_the_money_analysis(client, root):
    html = client.get("/film/demo").text
    node = flow_node(html, "cost")
    assert "video <b>$10.00</b>" in node and "lip sync <b>$2.50</b>" in node and "voices" not in node   # only what costs
    assert "s billed for" in node and "s on screen" in node                     # Veo bills the next clip length up
    shots = [{"duration_s": 1.7}] * 10                                         # ten short shots: 40 s billed for 17 s
    scenes = [{"blocks": [{"kind": "dialogue"}, {"kind": "action"}, {"kind": "dialogue"}]}]
    lines = films.cost_lines({"video_usd": 4.0, "tts_usd": 0.1}, {}, shots, [], scenes, [])
    assert [line["name"] for line in lines] == ["video", "voices"]
    assert lines[0]["why"].startswith("10 shots to film · 40 s billed for 17 s on screen") and "trimmed away" in lines[0]["why"]
    assert lines[1]["why"] == "2 lines"                                        # dialogue only, never the action
    long = [{"duration_s": 30.0, "characters_in_frame": ["a"]}]                 # as produce prices it: one clip, capped
    assert "8 s billed for 30 s on screen" in films.cost_lines({"video_usd": 1.0}, {}, long, [], [], [])[0]["why"]


def test_cast_cards_find_the_pictures_of_a_locked_library_character(client, root):
    """A real cast lock names its library characters as "characters/<id>@v<n>": the card shows that character's
    pictures, and a character still pending shows none."""
    lock_path = root / "demo" / "project" / "cast.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text())
    first, *rest = list(lock["characters"])
    lock["characters"][first]["character_asset"] = f"characters/{first}_x@v1"
    for cid in rest:
        lock["characters"][cid]["character_asset"] = f"characters/{cid}@pending"
    lock_path.write_text(yaml.safe_dump(lock))
    html = client.get("/film/demo").text
    assert html.count("01_front.png") >= 1


def finished_film(root):
    """The fixture film delivered: every shot filmed, the cut and the delivery there, the row done."""
    proj = root / "demo"
    (proj / "reviews" / "faces.pending").unlink(missing_ok=True)
    (proj / "11_master" / "deliver").mkdir(parents=True, exist_ok=True)
    (proj / "11_master" / "movie.mp4").write_bytes(b"")
    (proj / "11_master" / "deliver" / "delivery.json").write_text(json.dumps([{"name": "demo_720p.mp4", "mb": 9.0}]))
    for sh in (proj / "04_shots").glob("*.json"):
        for s in json.loads(sh.read_text())["shots"]:
            (proj / "inbox" / "video" / f"{s['shot_id']}.mp4").write_bytes(b"")
    rs = rows(root)
    rs[0]["state"] = "done"
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    return proj


def test_qa_reads_the_run_report_when_its_files_are_missing(client, root):
    """A runner film checked before the runner kept reports/qa/film.json: the run report still says how QA went, so
    QA never shows "todo" between a finished cut and a delivery."""
    proj = finished_film(root)
    report_path = proj / "reports" / "produce.json"
    report = json.loads(report_path.read_text())
    for steps, status, text in (({"qa_film": "look"}, "partial", "film check needs a look"),
                                ({"qa_film": "ok", "qa_lipsync": "2 of 9 shots flagged"}, "done",
                                 "film check ok; lip-sync: 2 of 9 shots flagged"),
                                ({"qa": "skipped: ImportError: no mediapipe"}, "skipped", "skipped: ImportError"),
                                ({}, "todo", "")):
        report.update(state="done", steps={"plan": "done", "cost": "approved", "faces": "approved", "render": "done",
                                           "publish": {"film": {"id": "v1"}}, **steps})
        report_path.write_text(json.dumps(report))
        node = flow_node(client.get("/film/demo").text, "qa")
        assert f'class="badge {status}">{status}' in node, (steps, node[:400])
        assert text in node


def test_the_cast_and_voices_stops_say_what_to_change(client, root):
    rs = rows(root)
    for note, said in (("characters ['chintu'] need identity packs (briefs in /work/projects/x/04_identity_briefs)",
                        "add cast.build_identity: true to its pipeline.yaml"),
                       ("voices: no ElevenLabs voice for ['chintu']; add voice.cast", "Add voice.cast.&lt;character&gt;")):
        rs[0].update(state=f"blocked:{note[:60]}", note=note)
        (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
        assert said in client.get("/film/demo").text
        rs[0].update(state=f"blocked:{note}", note="")                 # as the runner writes it: the reason in the state
        (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
        html = client.get("/film/demo").text
        assert said in html
        failed = html[html.index("Failed here:"):]                    # the card that carries the failure says it too
        failed = failed[:failed.index("</li>")]
        assert said in failed and "Re-run after the change" in failed and "Fix is in place" not in failed
        assert "Fix is in place" not in html                         # nor at the top of the page


def test_a_previous_cuts_qa_never_sits_between_open_steps_and_the_latest_run_wins(client, root):
    """A re-run film keeps the last cut's reports/qa/film.json until its own QA runs: while a step before QA is open
    again, that verdict is stale; and a latest run whose QA was skipped is not shown as the old cut's "ok"."""
    (root / "demo" / "reports" / "qa").mkdir(parents=True)
    (root / "demo" / "reports" / "qa" / "film.json").write_text(json.dumps({"ok": True}))
    html = client.get("/film/demo").text                     # the fixture waits at faces: QA is from a cut before
    assert 'class="badge stale">stale' in flow_node(html, "qa")
    report_path = root / "demo" / "reports" / "produce.json"
    report = json.loads(report_path.read_text())
    report["steps"]["qa"] = "skipped: ImportError: no mediapipe"
    report_path.write_text(json.dumps(report))
    assert films.detail(Store(str(root)), "demo", rows(root)[0])["stages"]["qa"]["status"] == "stale"
    # the film delivered again and its newest QA skipped: the runner's skipped file replaces the old "ok"
    finished_film(root)
    (root / "demo" / "reports" / "qa" / "film.json").write_text(json.dumps({"ok": None, "skipped": "ImportError: x"}))
    node = flow_node(client.get("/film/demo").text, "qa")
    assert 'class="badge skipped">skipped' in node and "film check ok" not in node


def test_the_qa_card_says_skipped_for_a_skipped_check(client, root):
    finished_film(root)
    (root / "demo" / "reports" / "qa" / "lipsync").mkdir(parents=True)
    (root / "demo" / "reports" / "qa" / "lipsync" / "summary.json").write_text(json.dumps([{"flags": ["lag"]}]))
    (root / "demo" / "reports" / "qa" / "film.json").write_text(json.dumps({"ok": None, "skipped": "ImportError: x"}))
    html = client.get("/film/demo").text
    assert 'Film check: <span class="badge skipped">skipped</span>' in html and "needs a look" not in html
    assert "lip-sync: 1 of 1" not in flow_node(html, "qa")                  # an older sheet, not this run's


def test_the_cast_cards_show_each_voice_and_the_owner_can_change_one(client, root):
    from urllib.parse import unquote
    proj = root / "demo"
    cfg = yaml.safe_load((proj / "project" / "pipeline.yaml").read_text())
    first = next(iter(yaml.safe_load((proj / "project" / "cast.lock.yaml").read_text())["characters"]))
    cfg["voice"] = {"cast": {first: "pickedVoice01"}}
    (proj / "project" / "pipeline.yaml").write_text(yaml.safe_dump(cfg))
    (proj / "reports" / "voices.json").write_text(json.dumps({first: {"voice_id": "pickedVoice01", "name": "George",
                                                                      "why": "male, middle aged, warm", "shared": False}}))
    html = card(client.get("/film/demo").text, "cast")
    assert "ElevenLabs: <b>George</b>" in html and "picked by the runner: male, middle aged, warm" in html
    assert 'action="/film/demo/voice"' in html and "Change voice" in html and "Set voice" in html
    r = client.post("/film/demo/voice", data={"character": first, "voice_id": "ownersVoice02"}, follow_redirects=False)
    assert "the runner uses it from its next run" in unquote(r.headers["location"])
    assert yaml.safe_load((root / "stories" / "demo.voices.yaml").read_text()) == {first: "ownersVoice02"}
    html = card(client.get("/film/demo").text, "cast")
    assert "ownersVoice02" in html and "your choice" in html
    for data, said in (({"character": first, "voice_id": "bad id!"}, "does not look like an ElevenLabs voice id"),
                       ({"character": "nobody", "voice_id": "ownersVoice03"}, "No character 'nobody'")):
        r = client.post("/film/demo/voice", data=data, follow_redirects=False)
        assert said in unquote(r.headers["location"])
    assert yaml.safe_load((root / "stories" / "demo.voices.yaml").read_text()) == {first: "ownersVoice02"}


def test_an_earlier_runs_block_after_an_open_step_is_stale(client, root):
    """While a re-run plans the shots again, the last run's "blocked" on a later step (the voices stop) is from that
    run: it reads stale, never as a live block."""
    report_path = root / "demo" / "reports" / "produce.json"
    report = json.loads(report_path.read_text())
    report["steps"]["render"] = "blocked: voices: no ElevenLabs voice for ['arjun']"
    report_path.write_text(json.dumps(report))
    node = flow_node(client.get("/film/demo").text, "render")                # the fixture waits at faces
    assert 'class="badge stale">stale' in node and 'class="badge blocked">' not in node


def test_a_speaker_without_a_voice_says_what_it_needs(client, root):
    assert "No voice yet" not in card(client.get("/film/demo").text, "cast")        # mock speech: nothing to set
    cfg_path = root / "demo" / "project" / "pipeline.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["providers"]["tts"] = "inbox"
    cfg["voice"] = {"inbox_fallback": "elevenlabs", "auto_cast": True}
    cfg_path.write_text(yaml.safe_dump(cfg))
    assert "No voice yet: the runner picks one at the cast step" in card(client.get("/film/demo").text, "cast")
    cfg["voice"]["auto_cast"] = False
    cfg_path.write_text(yaml.safe_dump(cfg))
    assert "the film stops before its cost gate until every speaker has one" in card(client.get("/film/demo").text, "cast")


def test_the_film_page_shows_what_stands_ahead(client, root):
    (root / "demo" / "reports" / "rehearsal.json").write_text(json.dumps({
        "checked": "2026-10-22T01:00:00+00:00",
        "stops": [{"step": "render", "what": "chintu is an animal with lines and no voice", "fix": "set a voice"}],
        "waste": [{"step": "shots", "what": "3 of 9 shots are off the 4/6/8 s clips", "fix": "re-plan the shots"}],
        "notes": [{"step": "publish", "what": "waits for you at: cost, publish", "fix": ""}]}))
    html = client.get("/film/demo").text
    ahead = html[html.index('id="ahead"'):html.index('id="flow"')]
    assert "Ahead of this film" in ahead and "chintu is an animal with lines and no voice" in ahead
    assert "→ set a voice" in ahead and "off the 4/6/8 s clips" in ahead and "waits for you at: cost, publish" in ahead
    assert ahead.index("Stops") < ahead.index("Waste") < ahead.index("Notes")
    (root / "demo" / "reports" / "rehearsal.json").write_text(json.dumps({"error": "KeyError: x"}))
    assert 'id="ahead"' not in client.get("/film/demo").text                 # a failed rehearsal is not news


def test_the_voices_stop_lands_on_the_voices_step(client, root):
    """The voices check runs right after the shot lists; its stop is the picture-and-voices step's, not theirs."""
    rs = rows(root)
    rs[0].update(state="blocked:voices: no ElevenLabs voice for ['chintu']", note="")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reviews" / "faces.pending").unlink()
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "state": "blocked", "phase": "voices"}))
    html = client.get("/film/demo").text
    assert "Failed here" in flow_node(html, "render") and "Failed here" not in flow_node(html, "shots")


def test_a_film_the_estimate_stopped_offers_one_click_to_raise_the_cap(client, root):
    """Over the cap: the cost step says so in plain words and offers a cap that fits (spent + estimate, a tenth to
    spare); one click saves it and re-runs, and the run stops at the cost gate for the owner's approval."""
    from urllib.parse import unquote
    rs = rows(root)
    rs[0].update(state="blocked:estimate $12.50 + spent $1.35 is over the cap $10.00; raise the cap or reduce the plan",
                 budget_usd="10", note="")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reviews" / "faces.pending").unlink()
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "state": "blocked", "phase": "cost"}))
    html = client.get("/film/demo").text
    node = flow_node(html, "cost")
    assert "Raise the cap to $16 and re-run" in node and 'action="/film/demo/cap-and-rerun"' in node   # ceil(13.85 x 1.1)
    assert "The paid stages cost more than this film" in html and "nothing is paid until you approve it" in html
    r = client.post("/film/demo/cap-and-rerun", data={"budget": "16"}, follow_redirects=False)
    said = unquote(r.headers["location"])
    assert "cap raised to 16 dollars" in said and "stops at the cost gate" in said
    row = rows(root)[0]
    assert row["budget_usd"] == "16" and row["state"] == "retry" and client.started == ["nraiorbit-runner"]
    r = client.post("/film/demo/cap-and-rerun", data={"budget": "20"}, follow_redirects=False)  # not stopped any more
    assert "not stopped by the estimate" in unquote(r.headers["location"]) and rows(root)[0]["budget_usd"] == "16"
    rs = rows(root)
    rs[0].update(state="blocked:BudgetExceeded: the ledger stopped the render")       # another stop (a stale page)
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    r = client.post("/film/demo/cap-and-rerun", data={"budget": "60"}, follow_redirects=False)
    assert "not stopped by the estimate" in unquote(r.headers["location"])
    assert rows(root)[0]["budget_usd"] == "16" and rows(root)[0]["state"].startswith("blocked:BudgetExceeded")
    assert client.post("/film/demo/cap-and-rerun", data={"budget": "-1"}).status_code == 400


def test_a_quota_stop_says_it_will_go_on_by_itself(client, root):
    rs = rows(root)
    rs[0].update(state="retry", note="quota (1): Google's quota was used up for now; everything already made is kept")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert "goes on from there at its next tick (every 15 minutes), up to three times" in html


def test_a_faces_hold_from_the_video_step_names_its_new_stills_on_the_film_page(root):
    """The video step held faces again for stills drawn while it ran: the faces card says which, not just a count,
    (a Retry keeping that list is tested in test_gates)."""
    named = ("New first frames were drawn while the video was made: sc008_s002, sc012_s002 (missing, or drawn again "
             "after Google refused the shot). Look at them in the control room, then approve faces; every clip "
             "already made is kept.\n")
    (root / "demo" / "reviews" / "faces.pending").write_text(named, encoding="utf-8")
    d = films.detail(Store(str(root)), "demo", rows(root)[0])
    assert d["stages"]["faces"]["status"] == "awaiting" and d["stages"]["faces"]["text"] == named.strip()
    (root / "demo" / "reviews" / "faces.pending").write_text("x", encoding="utf-8")
    assert films.detail(Store(str(root)), "demo", rows(root)[0])["stages"]["faces"]["text"].endswith("a first frame")


def test_a_reference_retry_changes_only_the_blocked_row_clicked(client, root):
    """The same link queued for two focuses: Retry on the blocked one must not flip the other, whether it is done
    (re-learning is paid again) or being learned right now (a second tick would learn it twice)."""
    url = "https://www.youtube.com/watch?v=abc123xyz"
    base = {"url": url, "title": "Ref", "minutes": "", "profile_uri": "", "note": "", "updated": "t0"}
    for other in ("learning", "done"):
        (root / "queue" / "references.csv").write_text(csv_text([
            {**base, "learn": "score", "state": "blocked:LearnCostError"}, {**base, "learn": "trailer", "state": other}],
            queue.REFERENCE_COLUMNS))
        client.post("/references/retry", data={"url": url, "learn": "score"})
        assert [r["state"] for r in rows(root, queue.REFERENCE_COLUMNS, "references.csv")] == ["retry", other]
        client.post("/references/retry", data={"url": url, "learn": "trailer"})   # a stale page's button
        assert [r["state"] for r in rows(root, queue.REFERENCE_COLUMNS, "references.csv")] == ["retry", other]
    (root / "queue" / "references.csv").write_text(csv_text([{**base, "learn": "score", "state": "blocked:X"}],
                                                            queue.REFERENCE_COLUMNS))
    assert 'name="learn" value="score"' in client.get("/references").text      # the button names its focus


def test_send_back_writes_the_request_and_puts_the_film_back_in_the_queue(client, root):
    """The film waits at a gate: the page offers Send back next to Approve; the owner's notes, the step and the shots
    go beside the story for the next tick, and the row goes back to retry. A step, notes and real shot ids only."""
    page = client.get("/film/demo").text
    assert 'action="/film/demo/rework"' in page and "Send back for rework" in page
    assert '<option value="render" selected>' in page                         # the faces gate points at the video
    first = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0].shot_id
    for data, says in [({"stage": "nonsense", "notes": "x"}, "Pick the step"),
                       ({"stage": "render", "notes": "  "}, "Write what needs fixing"),
                       ({"stage": "shots", "shots": first, "notes": "x"}, "Shot ids go with"),
                       ({"stage": "render", "shots": "sc999_s001", "notes": "x"}, "No shot sc999_s001")]:
        r = client.post("/film/demo/rework", data=data, follow_redirects=True)
        assert says in r.text and rows(root)[0]["state"] == "awaiting:faces"
        assert not (root / "stories" / "demo.rework.json").exists()
    r = client.post("/film/demo/rework", data={"stage": "render", "notes": "remake it all"}, follow_redirects=True)
    assert "Name the shots to redo" in r.text and rows(root)[0]["state"] == "awaiting:faces"
    r = client.post("/film/demo/rework", data={"stage": "render", "shots": f"{first}, {first}",
                                               "notes": "keep the child out of frame"}, follow_redirects=True)
    assert "Sent back" in r.text
    ask = json.loads((root / "stories" / "demo.rework.json").read_text())
    assert ask["stage"] == "render" and ask["shots"] == [first] and ask["notes"] == "keep the child out of frame"
    assert ask["id"] and ask["by"]
    row = rows(root)[0]
    assert row["state"] == "retry" and row["note"].startswith("sent back to Picture, voices, music, lips by ")


def test_send_back_never_lands_on_a_run_under_way(client, root):
    rs = rows(root)
    rs[0].update(state="producing", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    r = client.post("/film/demo/rework", data={"stage": "trailer", "notes": "shorter"}, follow_redirects=True)
    assert "the runner is on it" in r.text and rows(root)[0]["state"] == "producing"
    assert not (root / "stories" / "demo.rework.json").exists()                # refused before anything is written


def test_a_second_send_back_before_the_tick_keeps_the_first(client, root):
    client.post("/film/demo/rework", data={"stage": "trailer", "notes": "shorter"}, follow_redirects=True)
    first = (root / "stories" / "demo.rework.json").read_text()
    r = client.post("/film/demo/rework", data={"stage": "scenes", "notes": "redo it all"}, follow_redirects=True)
    assert "the runner is on it" in r.text                                       # retry: the tick is about to run
    assert (root / "stories" / "demo.rework.json").read_text() == first          # the first one still goes



def test_after_a_send_back_the_page_shows_the_shots_new_still_and_not_its_old_clip_as_filmed(client, root):
    """A Send back moves a shot on to take 2: the faces card must show take_2.png (the still to approve), and the old
    take's clip must not make the shot look filmed."""
    first = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0].shot_id
    assert (root / "demo" / "inbox" / "video" / f"{first}.mp4").exists()
    before = client.get("/film/demo").text
    assert f"{first}/take_1.png" in before
    (root / "demo" / "06_keyframes" / first / "take_2.png").write_bytes(png())
    (root / "demo" / "project" / "takes.yaml").write_text(f"{first}: 2\n")
    d = films.detail(Store(str(root)), "demo", rows(root)[0])
    card = next(c for c in d["keyframes"] if c["shot_id"] == first)
    assert "take_2.png" in card["url"] and card["filmed"] is False
    (root / "demo" / "inbox" / "video" / f"{first}_2.mp4").write_bytes(b"")
    d = films.detail(Store(str(root)), "demo", rows(root)[0])
    assert next(c for c in d["keyframes"] if c["shot_id"] == first)["filmed"] is True


def test_the_delivery_card_reads_what_deliver_writes(client, root):
    """deliver() writes {"date", "files": [...]}; the page used to expect a bare list and showed nothing."""
    (root / "demo" / "11_master" / "deliver").mkdir(parents=True, exist_ok=True)
    (root / "demo" / "11_master" / "deliver" / "delivery.json").write_text(json.dumps({"date": "2026-10-22", "files": [
        {"name": "Rain_720p.mp4", "gs": "gs://b/demo/final/Rain_720p.mp4", "url": "https://x", "mb": 120.5}]}))
    d = films.detail(Store(str(root)), "demo", rows(root)[0])
    assert [x["name"] for x in d["delivery"]] == ["Rain_720p.mp4"]                 # the card lists it


def _at_review(root, gate, state=None):
    """The film cut and waiting at `gate` (or in `state`)."""
    (root / "demo" / "11_master").mkdir(exist_ok=True)
    (root / "demo" / "11_master" / "movie.mp4").write_bytes(b"film")
    (root / "demo" / "reviews" / "faces.pending").unlink(missing_ok=True)
    if gate:
        (root / "demo" / "reviews" / f"{gate}.pending").write_text("x")
    rs = rows(root)
    rs[0].update(state=state or f"awaiting:{gate}", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))


def test_reject_sits_beside_approve_at_the_cut_and_trailer_reviews_and_saves_the_note_for_the_editor(client, root):
    assert 'action="/film/demo/reject"' not in client.get("/film/demo").text   # faces: Send back, not Reject
    _at_review(root, "trailer")
    page = client.get("/film/demo").text
    assert 'action="/film/demo/reject"' in page and 'name="gate" value="trailer"' in page and "Reject…" in page
    r = client.post("/film/demo/reject", data={"gate": "trailer", "notes": "  "}, follow_redirects=True)
    assert "Write what is wrong" in r.text and not (root / "stories" / "demo.rework.json").exists()
    r = client.post("/film/demo/reject", data={"gate": "trailer", "notes": "open on the boat chase"},
                    follow_redirects=True)
    assert "the AI editor reads your note" in r.text
    ask = json.loads((root / "stories" / "demo.rework.json").read_text())
    assert ask["mode"] == "triage" and ask["gate"] == "trailer" and ask["notes"] == "open on the boat chase"
    assert ask["id"] and ask["by"] == "owner@example.com" and "stage" not in ask
    row = rows(root)[0]
    assert row["state"] == "retry" and row["note"].startswith("rejected at trailer by owner@example.com")
    page = client.get("/film/demo").text
    assert "the AI editor is deciding what to redo" in page and 'action="/film/demo/reject"' not in page
    rs = rows(root)                                                     # the editor could not be reached
    rs[0].update(state="blocked:LLMError", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    assert "Retry runs your rejection again" in client.get("/film/demo").text
    _at_review(root, "trailer")                                         # back at the review, never applied
    page = client.get("/film/demo").text
    assert "was not applied" in page and 'action="/film/demo/reject"' in page


def test_reject_is_offered_on_a_finished_film_and_never_on_a_page_the_film_has_left(client, root):
    _at_review(root, None, "done")
    assert 'name="gate" value="done"' in client.get("/film/demo").text
    r = client.post("/film/demo/reject", data={"gate": "final_cut", "notes": "too long"}, follow_redirects=True)
    assert "not at the review this page showed" in r.text and rows(root)[0]["state"] == "done"
    assert not (root / "stories" / "demo.rework.json").exists()                 # refused before anything is written
    _at_review(root, "final_cut", "producing")
    r = client.post("/film/demo/reject", data={"gate": "final_cut", "notes": "too long"}, follow_redirects=True)
    assert "the runner is on it" in r.text and rows(root)[0]["state"] == "producing"


def test_reject_stops_at_six_a_day(client, root):
    _at_review(root, "final_cut")
    today = queue.now()
    notes = [{"id": f"r{i}", "mode": "triage", "at": today} for i in range(6)]
    (root / "demo" / "reports" / "owner_notes.jsonl").write_text("".join(json.dumps(n) + "\n" for n in notes))
    r = client.post("/film/demo/reject", data={"gate": "final_cut", "notes": "too long"}, follow_redirects=True)
    assert "6 rejections today" in r.text and rows(root)[0]["state"] == "awaiting:final_cut"


def test_the_page_shows_what_the_editor_decided(client, root):
    _at_review(root, "trailer")
    record = {"id": "t1", "mode": "triage", "gate": "trailer", "owner_notes": "the music drowns the lines",
              "understood": "The score is too loud under the dialogue.", "why": "A quieter score keeps every line.",
              "question": "", "needs_code_change": True, "stage": "assemble", "applied": "2026-10-23T10:00:00+00:00",
              "actions": [{"route": "mix", "shots": [], "notes": "music down 6 dB"},
                          {"route": "reshoot_shots", "shots": ["sc002_s001"], "notes": "the hero alone"}],
              "what": "the sound is mixed again (music -6 dB from the original)"}
    (root / "demo" / "reports" / "owner_notes.jsonl").write_text(json.dumps(record) + "\n")
    (root / "demo" / "reports" / "trailer_direction.json").write_text(json.dumps(
        {"rework_id": "t1", "direction": {}, "unmet": ["shot sc006_s003 is not in the trailer"]}))
    page = client.get("/film/demo").text
    for says in ("The AI editor's decision", "rejected at the trailer review", "the music drowns the lines",
                 "The score is too loud under the dialogue.", "Mix the sound again", "Remake shots", "sc002_s001",
                 "A quieter score keeps every line.", "music -6 dB from the original",
                 "shot sc006_s003 is not in the trailer", "needs a code change"):
        assert says in page, says
    record.update(actions=[], question="Which scene do you mean?", needs_code_change=False)
    (root / "demo" / "reports" / "owner_notes.jsonl").write_text(json.dumps(record) + "\n")
    page = client.get("/film/demo").text
    assert "Nothing was redone. Which scene do you mean?" in page and "is not in the trailer" in page
    record.update(skipped="the film had moved on from the trailer review")
    (root / "demo" / "reports" / "owner_notes.jsonl").write_text(json.dumps(record) + "\n")
    assert "Not applied: the film had moved on from the trailer review." in client.get("/film/demo").text
    later = {"id": "s1", "stage": "shots", "notes": "fewer close-ups"}                # a Send back takes its place
    (root / "demo" / "reports" / "owner_notes.jsonl").write_text(json.dumps(record) + "\n" + json.dumps(later) + "\n")
    assert "The AI editor's decision" not in client.get("/film/demo").text


def test_a_stopped_run_that_beat_is_resumed_within_minutes_not_hours(client, root, monkeypatch):
    """Cloud Run stopped the run (preempted) five minutes in. Its record beat until then, so the next check after
    STALL_S takes the film over, and the page says when, instead of six hours after the claim."""
    import datetime
    from zoneinfo import ZoneInfo
    from nraiorbit.service.progress import STALL_S
    claim = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) - datetime.timedelta(minutes=40)
    beat = claim + datetime.timedelta(minutes=5)
    rs = rows(root)
    rs[0].update(state="producing", updated=claim.isoformat())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps(
        {**PROGRESS, "execution": "nraiorbit-runner-old", "phase": "render", "started": claim.isoformat(),
         "claim": claim.isoformat(), "beat": beat.isoformat()}))
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [running_exec("nraiorbit-runner-old", state="failed")])
    html = client.get("/film/demo").text
    resumes = (beat + datetime.timedelta(seconds=STALL_S)).astimezone(ZoneInfo(Settings().timezone))
    assert "stopped without finishing" in html and "Re-run it now" in html
    assert f"at its first check after {resumes:%Y-%m-%d %H:%M}" in html and "every 15 minutes" in html


def test_the_steps_a_live_run_is_past_are_never_shown_failed_and_an_old_cut_is_never_shown_done(root):
    """The row still carries the last run's stop (render blocked) while a new run is cutting the film: the steps
    before the one under way passed in this run. While it renders, the cut from an earlier run is not "done"."""
    (root / "demo" / "reviews" / "faces.pending").unlink()
    (root / "demo" / "11_master").mkdir()
    (root / "demo" / "11_master" / "movie.mp4").write_bytes(b"film")
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps(
        {**PROGRESS, "execution": "nraiorbit-runner-live", "phase": "trailer"}))
    store = Store(str(root))
    row = {**rows(root)[0], "state": "producing"}
    live = films.detail(store, "demo", row, running={"nraiorbit-runner-live"})["stages"]
    assert live["assemble"]["status"] == "executing"
    assert live["render"] == {"status": "done", "text": "passed in this run"}
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps(
        {**PROGRESS, "execution": "nraiorbit-runner-live", "phase": "render"}))
    earlier = films.detail(store, "demo", row, running={"nraiorbit-runner-live"})["stages"]
    assert earlier["render"]["status"] == "executing" and earlier["assemble"]["status"] == "stale"   # remade next


def test_a_message_is_shown_once_and_dropped_from_the_address(client):
    """A reload (or the page's own refresh) of /film/demo?msg=... must not show an old "approved" again."""
    html = client.get("/film/demo?msg=cost%20approved").text
    assert "cost approved" in html and 'searchParams.delete("msg")' in html
    assert 'searchParams.delete("msg")' not in client.get("/film/demo").text


def test_saving_a_producing_films_settings_keeps_the_runs_claim(client, root):
    """A producing row's time names the run that claimed it (the runner takes a silent run over by it); a save of the
    cap or visibility while it runs changes those, never the claim."""
    rs = rows(root)
    rs[0].update(state="producing", updated="2026-10-25T04:16:04+00:00")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    client.post("/film/demo/row", data={"budget": "75", "publish": "unlisted"}, follow_redirects=False)
    row = rows(root)[0]
    assert row["budget_usd"] == "75" and row["publish"] == "unlisted" and row["updated"] == "2026-10-25T04:16:04+00:00"


def _reels(root, n=3):
    folder = root / "demo" / "11_master" / "reels"
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(1, n + 1):
        (folder / f"reel_{i}.mp4").write_bytes(b"reel")
        made.append({"file": f"reel_{i}.mp4", "start_s": 10.0 * i, "duration_s": 5.0, "line_id": None, "line_te": "",
                     "line_en": f"line {i}", "caption": f"caption {i}"})
    (folder / "reels.json").write_text(json.dumps({"film": "x", "reels": made}))


def test_the_film_page_offers_each_reel_to_download_with_its_caption(client, root):
    assert 'id="reels"' not in client.get("/film/demo").text
    _reels(root)
    html = client.get("/film/demo").text
    assert 'id="reels"' in html and "Download reel 3" in html and "caption 2" in html and "Copy caption" in html
    assert 'href="/file/demo/11_master/reels/reel_1.mp4" download' in html


def test_shorts_go_up_only_when_the_owner_asks_and_never_twice(client, root):
    from urllib.parse import unquote
    r = client.post("/film/demo/shorts", data={"privacy": "public"}, follow_redirects=False)
    assert "no reels yet" in unquote(r.headers["location"]) and client.started == []
    _reels(root)
    html = client.get("/film/demo").text
    assert "Connect YouTube to upload the reels as Shorts" in html and 'action="/film/demo/shorts"' not in html
    (root / "settings").mkdir(exist_ok=True)
    (root / "settings" / "youtube.json").write_text(json.dumps({"channel": "NRAIORBIT"}))
    rs = rows(root)
    rs[0]["publish"] = "unlisted"
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert 'action="/film/demo/shorts"' in html and "Upload the 3 reels to YouTube as Shorts" in html
    assert '<option value="unlisted" selected>' in html                      # the film's own setting by default
    r = client.post("/film/demo/shorts", data={"privacy": "sideways"}, follow_redirects=False)
    assert "Pick private, unlisted or public" in unquote(r.headers["location"]) and client.started == []
    r = client.post("/film/demo/shorts", data={"privacy": "public"}, follow_redirects=False)
    assert r.status_code == 303 and client.started == ["nraiorbit-runner"]
    ask = json.loads((root / "stories" / "demo.shorts.json").read_text())
    assert ask["privacy"] == "public" and ask["by"] == "owner@example.com"
    assert "You asked for the Shorts" in client.get("/film/demo").text
    (root / "stories" / "demo.shorts.json").unlink()                          # the runner took it and is uploading
    from nraiorbit.service import shorts as shorts_mod
    (root / "demo" / "reports" / "youtube_shorts.json").write_text(json.dumps({"uploading": {"at": shorts_mod._now()}}))
    assert "You asked for the Shorts" in client.get("/film/demo").text       # no button while they go up
    r = client.post("/film/demo/shorts", data={"privacy": "public"}, follow_redirects=False)
    assert "going up now" in unquote(r.headers["location"]) and not (root / "stories" / "demo.shorts.json").exists()
    (root / "demo" / "reports" / "youtube_shorts.json").write_text(json.dumps(
        {"privacy": "public", "reel_1": {"id": "S1", "film": "x"}, "reel_2": {"id": "OldCutShort", "film": "an older cut"},
         "error": "RuntimeError: quotaExceeded"}))
    html = client.get("/film/demo").text
    assert "youtube.com/shorts/S1" in html and "OldCutShort" not in html and "Upload the 2 reels" in html
    assert "quotaExceeded" in html


def test_the_top_of_the_page_says_what_needs_you_and_what_the_run_did_in_plain_words(client, root, monkeypatch):
    """Only what needs the owner, each with its step, what it is for and its button; then what the run is doing
    and the steps it finished. The step tiles are in the pipeline below, not here."""
    html = client.get("/film/demo").text
    top = html[html.index("What needs you"):html.index('id="ahead"') if 'id="ahead"' in html else html.index('id="flow"')]
    assert "Step 7 · First frames" in top and "The video is made from these" in top and "See the first frames" in top
    assert 'class="stage ' not in top
    (root / "demo" / "reviews" / "faces.pending").unlink()
    rs = rows(root)
    rs[0].update(state="done")
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "state": "done",
        "updated": "2026-10-19T07:30:00+00:00", "history": [{"phase": "render", "done": "2026-10-19T07:00:00+00:00"},
                                                           {"phase": "reels", "done": "2026-10-19T07:10:00+00:00"}]}))
    html = client.get("/film/demo").text
    assert "Nothing needs you right now." in html and "The last run finished on 2026-10-19 03:30 EDT" in html
    assert html.index("Cut the reels") < html.index("Made the picture, voices, music and lip-sync")   # newest first
    assert "Send back for rework" in html and "choose the step to redo yourself" in html
    for odd in (7, True, [{"phase": ["render"]}, {"phase": {"x": 1}}, "text"]):    # never a crash on an odd record
        (root / "demo" / "reports" / "progress.json").write_text(json.dumps({**PROGRESS, "state": "done", "history": odd}))
        assert client.get("/film/demo").status_code == 200


def test_the_owner_chooses_whether_shorts_follow_the_film_and_the_top_says_where_the_reels_are(client, root):
    html = client.get("/film/demo").text
    assert '<option value="auto" selected>Shorts: automatic' in html and "Instagram is yours" in html
    r = client.post("/film/demo/shorts-mode", data={"mode": "ask"}, follow_redirects=False)
    assert r.status_code == 303 and json.loads((root / "stories" / "demo.shorts-mode.json").read_text())["mode"] == "ask"
    assert '<option value="ask" selected>' in client.get("/film/demo").text
    from urllib.parse import unquote
    r = client.post("/film/demo/shorts-mode", data={"mode": "sometimes"}, follow_redirects=False)
    assert "nothing changed" in unquote(r.headers["location"])
    _reels(root)
    html = client.get("/film/demo").text
    assert "Reels: 3 ready for Instagram" in html and "waiting for your button" in html and "Step 12 · YouTube" in html



def test_switching_a_film_already_on_youtube_to_automatic_sends_its_reels_now_with_the_films_visibility(client, root):
    _reels(root)
    (root / "demo" / "reports" / "youtube.json").write_text(json.dumps({"film": {"id": "F", "privacy": "unlisted"}}))
    client.post("/film/demo/shorts-mode", data={"mode": "auto"}, follow_redirects=False)
    ask = json.loads((root / "stories" / "demo.shorts.json").read_text())
    assert ask["privacy"] == "unlisted" and ask["film"] == "x" and client.started == ["nraiorbit-runner"]
    (root / "stories" / "demo.shorts.json").unlink()
    (root / "demo" / "reports" / "youtube.json").unlink()                    # not on YouTube: only the setting
    client.post("/film/demo/shorts-mode", data={"mode": "auto"}, follow_redirects=False)
    assert not (root / "stories" / "demo.shorts.json").exists()



def test_references_are_priced_from_their_length_and_a_costly_one_waits_for_the_owners_approval(client, root,
                                                                                               monkeypatch):
    from urllib.parse import unquote

    from nraiorbit.service import learn
    monkeypatch.setattr(learn, "video_minutes", lambda url, fetch=None: 12.6)
    client.post("/references", data={"url": "https://www.youtube.com/watch?v=CEvTjeXOC3U", "learn": "all"},
                follow_redirects=False)
    rows_ = queue.load(str(root / "queue" / "references.csv"), queue.REFERENCE_COLUMNS)
    assert rows_[0]["minutes"] == "12.6"                                     # read from the video's page
    html = client.get("/references").text
    assert "Costs about <b>$0.1" in html and "(12.6 min)" in html
    rows_[0].update(state="blocked:LearnCostError", minutes="", note="over the $1.00 limit")
    queue.save(str(root / "queue" / "references.csv"), rows_, queue.REFERENCE_COLUMNS)
    html = client.get("/references").text
    assert "Approve $2.21 and watch it" in html and "priced as a 150 min film" in html and "Retry</button>" not in html
    rows_[0]["note"] = "watching this 170 min video costs about $2.50 (950,000 tokens), over the $1.00 limit"
    queue.save(str(root / "queue" / "references.csv"), rows_, queue.REFERENCE_COLUMNS)
    html = client.get("/references").text                                   # the runner found the real length
    assert "Approve $2.50 and watch it" in html and "(170 min)" in html
    r = client.post("/references/approve", data={"url": rows_[0]["url"], "learn": "all", "usd": "2.50"},
                    follow_redirects=False)
    row = queue.load(str(root / "queue" / "references.csv"), queue.REFERENCE_COLUMNS)[0]
    assert row["state"] == "retry" and row["approved_usd"] == "2.75" and client.started == ["nraiorbit-runner"]
    r = client.post("/references/approve", data={"url": rows_[0]["url"], "learn": "all", "usd": "2.50"},
                    follow_redirects=False)
    assert "no longer waiting" in unquote(r.headers["location"])             # approved once
    rows_ = queue.load(str(root / "queue" / "references.csv"), queue.REFERENCE_COLUMNS)
    rows_[0].update(state="blocked:LearnCostError", note="watching this 2000 min video costs about $40.00 (x tokens)")
    queue.save(str(root / "queue" / "references.csv"), rows_, queue.REFERENCE_COLUMNS)
    r = client.post("/references/approve", data={"url": rows_[0]["url"], "learn": "all", "usd": "1.00"},
                    follow_redirects=False)
    assert "the price is now $40.00" in unquote(r.headers["location"])      # the server's price, not the form's
    r = client.post("/references/approve", data={"url": rows_[0]["url"], "learn": "all", "usd": "40.00"},
                    follow_redirects=False)
    assert "over the $25" in unquote(r.headers["location"])
    assert queue.load(str(root / "queue" / "references.csv"), queue.REFERENCE_COLUMNS)[0]["state"] == \
        "blocked:LearnCostError"
    rows_[0].update(note="watching this video (length not known, priced as 150 min) costs about $2.21")
    queue.save(str(root / "queue" / "references.csv"), rows_, queue.REFERENCE_COLUMNS)
    assert "priced as a 150 min film" in client.get("/references").text     # a guess never reads as measured
