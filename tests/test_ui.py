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
    r = anon.get("/")
    assert r.status_code == 403 and "No signed-in account" in r.text
    r = anon.get("/", headers={IAP: "other@example.com"})
    assert r.status_code == 403 and "other@example.com" in r.text and "owner@example.com" in r.text
    assert anon.get("/", headers={IAP: "bad"}).status_code == 403                 # an assertion that does not verify
    # the plain e-mail header alone is never enough: without IAP in front anyone could send it
    assert anon.get("/", headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:owner@example.com"}).status_code == 403
    assert anon.get("/", headers={IAP: "Owner@Example.com"}).status_code == 200
    local = TestClient(create_app(Store(str(root)), Settings(auth=False)))
    assert local.get("/").status_code == 200                                    # a laptop, no sign-in


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
    assert r.status_code == 303 and "approved" in r.headers["location"]
    row = rows(root)[0]
    assert row["state"] == "approved" and "owner@example.com" in row["note"]
    client.post("/film/demo/row", data={"budget": "75", "publish": "unlisted", "gates": ""})
    row = rows(root)[0]
    assert row["budget_usd"] == "75" and row["publish"] == "unlisted" and row["gates"] == "ask"
    assert client.post("/film/demo/row", data={"budget": "-5"}).status_code == 400
    client.post("/film/demo/retry")
    assert rows(root)[0]["state"] == "retry"
    assert not (root / "demo" / "reviews" / "faces.approved").exists()          # the runner does the file work
    r = client.post("/tick", data={"next": "/film/demo"}, follow_redirects=False)
    assert r.status_code == 303 and client.started == ["nraiorbit-runner"]


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
    # guards: a bad slug, a tiny story, no cap, a duplicate
    bad = dict(slug="Rain Ferry", story=story, budget="10")
    assert client.post("/new", data=bad).status_code == 400
    assert client.post("/new", data={**bad, "slug": "ok-slug", "story": "short"}).status_code == 400
    assert client.post("/new", data={**bad, "slug": "ok-slug", "budget": "0"}).status_code == 400
    assert client.post("/new", data={**bad, "slug": "demo"}).status_code == 400


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
