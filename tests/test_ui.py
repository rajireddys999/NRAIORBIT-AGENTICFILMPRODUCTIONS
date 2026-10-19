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
    assert "Approve faces and continue" in html
    assert "Waiting at the <b>faces</b> gate" in html
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
    client.post("/film/demo/retry")
    assert rows(root)[0]["state"] == "retry"
    assert not (root / "demo" / "reviews" / "faces.approved").exists()          # the runner does the file work
    r = client.post("/tick", data={"next": "/film/demo"}, follow_redirects=False)
    assert r.status_code == 303 and client.started == ["nraiorbit-runner", "nraiorbit-runner"]


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
    assert "<b>Animatic.</b>" in html and "no first frames: mock video" in html and "nothing to pay: mock providers" in html
    assert 'class="badge skipped">skipped' in html and "film (animatic)" in html and 'class="badge stale">' not in html
    assert 'class="badge held">animatic' in client.get("/").text
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


def test_a_run_under_way_offers_no_button_and_a_stale_click_changes_nothing(client, root):
    """While a tick renders, the bucket still holds the previous stop's report and marker (awaiting:faces,
    faces.pending). The page must not offer that gate again, the board neither, and a click from a stale page must
    not turn the runner's claim back into an approval: that would let the next scheduled tick pay for the step twice."""
    rs = rows(root)
    rs[0].update(state="producing", updated=queue.now())
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert 'name="gate"' not in html and "Approve faces" not in html and "The runner is on it" in html
    assert "Approve faces" not in client.get("/").text
    r = client.post("/film/demo/approve", data={"gate": "faces"}, follow_redirects=False)
    assert r.status_code == 303 and "nothing waits" in r.headers["location"].replace("%20", " ")
    assert rows(root)[0]["state"] == "producing" and client.started == []


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
    assert "executing now" in html and 'class="badge next live">executing</span>' in html


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
    assert "Running <b>Scenes &amp; dialogue</b> since 02:48 EDT · last: sc010: 3 lines (" in html
    assert "execution <span class=\"mono\">nraiorbit-runner-mfkkm</span>" in html
    assert 'class="node executing"' in html and 'class="badge executing live">executing</span>' in html
    assert 'class="stage executing"' in html and "executing now · scenes" in html
    assert '<noscript><meta http-equiv="refresh" content="20"></noscript>' in html and "sessionStorage" in html
    assert "location.reload()" in html and "pagehide" not in html             # only its own refresh keeps the place
    assert 'name="gate"' not in html                                           # a run under way offers no button
    rs = rows(root)
    rs[0]["state"] = "approved"                                                 # approved, the tick not yet claimed
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert 'name="gate"' not in html and "Approve" not in html and "Running <b>" in html
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
    assert "executing now · " in html and "on the next tick" not in html      # a run is on the film: never "next tick"
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
