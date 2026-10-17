"""Lip sync through sync.so: staging with signed links, submit/poll/download, conforming, caching, budget, failures."""

import json
from types import SimpleNamespace

import pytest

from nraiorbit.costing import BudgetExceeded, SpendLedger
from nraiorbit.post import ffmpeg
from nraiorbit.providers import sync_so
from nraiorbit.providers.sync_so import SyncSoError, SyncSoLipSync, _output_url

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")


class FakeBlob:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def upload_from_filename(self, filename, content_type=None):
        self.store[self.path] = open(filename, "rb").read()

    def generate_signed_url(self, version, expiration, method):
        return f"https://storage.example/{self.path}?signed=1"


class FakeStorage:
    def __init__(self):
        self.store = {}

    def bucket(self, name):
        return SimpleNamespace(blob=lambda path: FakeBlob(self.store, f"{name}/{path}"))


class FakeResponse:
    def __init__(self, status_code, payload=None, body=b""):
        self.status_code, self._payload, self.content, self.text = status_code, payload, body, str(payload)

    def json(self):
        return self._payload


class FakeSyncSo:
    """Plays sync.so: a generation is PROCESSING for `polls` status calls, then COMPLETED (or FAILED for fail_ids)."""

    def __init__(self, polls=2, fail=False, no_id=False):
        self.polls, self.fail, self.no_id = polls, fail, no_id
        self.posts, self.gets, self.status_calls = [], [], {}

    def post(self, url, timeout=None, json=None):
        self.posts.append((url, json))
        if self.no_id:
            return FakeResponse(200, {"status": "PENDING"})
        gid = f"gen-{len(self.posts)}"
        self.status_calls[gid] = 0
        return FakeResponse(201, {"id": gid, "status": "PENDING"})

    def get(self, url, timeout=None):
        self.gets.append(url)
        gid = url.rsplit("/", 1)[-1]
        if gid not in self.status_calls:
            return FakeResponse(404, {"message": "No generation was found with that id.",
                                      "errorCode": "generation_not_found"})
        self.status_calls[gid] += 1
        if self.status_calls[gid] < self.polls:
            return FakeResponse(200, {"id": gid, "status": "PROCESSING"})
        if self.fail:
            return FakeResponse(200, {"id": gid, "status": "FAILED", "error": "no face found"})
        return FakeResponse(200, {"id": gid, "status": "COMPLETED", "outputUrl": f"https://cdn.example/{gid}.mp4"})


def _clip(tmp_path, name, color, seconds=2):
    path = tmp_path / f"{name}.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=24:d={seconds}", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(path)])
    wav = tmp_path / f"{name}.wav"
    ffmpeg.run(["-f", "lavfi", "-t", str(seconds), "-i", "sine=f=220:r=48000", str(wav)])
    return path, wav


def _provider(tmp_path, api, budget=None, **ls):
    cfg = {"lipsync": {"project": "film-proj", "bucket": "film-media", "poll_seconds": 0, **ls}}
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", budget)
    prov = SyncSoLipSync(tmp_path, cfg, {"sync_so": {"per_second": {"sync-3": 0.133}}}, ledger, FakeStorage(), api)
    return prov, ledger


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_so, "SLEEP", lambda s: None)
    # the "download" serves the staged input video back (a stand-in for the synced result), 1 s longer than the clip
    def fake_download(self, url, out):
        src = next(v for v in self._served.values())
        ffmpeg.run(["-i", str(src), "-vf", "tpad=stop_mode=clone:stop_duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(out)])
    monkeypatch.setattr(SyncSoLipSync, "_download", fake_download)


def test_submits_polls_downloads_conforms_caches_and_logs(tmp_path):
    v, a = _clip(tmp_path, "a", "red")
    api = FakeSyncSo(polls=3)
    prov, ledger = _provider(tmp_path, api, budget=10.0)
    prov._served = {"a": v}
    out = prov.apply(v, a, tmp_path / "out" / "a_sync.mp4")
    assert out and out.exists()
    # staged with signed links and the right body
    url, body = api.posts[0]
    assert url.endswith("/v2/generate") and body["model"] == "sync-3"
    assert [i["type"] for i in body["input"]] == ["video", "audio"]
    assert all(i["url"].startswith("https://storage.example/film-media/lipsync/syncso/") for i in body["input"])
    assert body["options"] == {"sync_mode": "cut_off"}
    assert len(api.gets) == 3                                 # polled until COMPLETED
    # conformed to the input: same frame count although the result was longer
    assert ffmpeg.frame_count(out) == ffmpeg.frame_count(v) == 48
    # cached: the second call makes no request and pays nothing
    posts = len(api.posts)
    assert prov.apply(v, a, tmp_path / "out" / "again.mp4").exists() and len(api.posts) == posts
    rows = ledger.records()
    assert len(rows) == 1 and rows[0]["provider"] == "sync_so" and rows[0]["usd"] == pytest.approx(2 * 0.133, abs=1e-3)
    assert rows[0]["generation"] == "gen-1"


def test_budget_stops_before_submitting(tmp_path):
    v, a = _clip(tmp_path, "b", "blue")
    api = FakeSyncSo()
    prov, _ = _provider(tmp_path, api, budget=0.10)          # 2 s x 0.133 = 0.27 > 0.10
    prov._served = {"b": v}
    with pytest.raises(BudgetExceeded):
        prov.apply(v, a, tmp_path / "out.mp4")
    assert api.posts == []


def test_failed_generation_stops_or_is_remembered(tmp_path):
    v, a = _clip(tmp_path, "c", "green")
    prov, _ = _provider(tmp_path, FakeSyncSo(fail=True))
    prov._served = {"c": v}
    with pytest.raises(SyncSoError, match="no face found"):
        prov.apply(v, a, tmp_path / "out.mp4")
    api = FakeSyncSo(fail=True)
    prov, ledger = _provider(tmp_path, api, on_failure="keep_original")
    prov._served = {"c": v}
    assert prov.apply(v, a, tmp_path / "out.mp4") is None
    assert prov.failure(v, a) == "no face found"
    assert prov.apply(v, a, tmp_path / "out.mp4") is None and len(api.posts) == 1   # not sent again
    assert ledger.records() == []                             # a failed job is not charged


def test_concurrency_submits_several_at_once(tmp_path):
    v1, a1 = _clip(tmp_path, "d", "red")
    v2, a2 = _clip(tmp_path, "e", "blue")
    api = FakeSyncSo(polls=2)
    prov, _ = _provider(tmp_path, api, concurrency=2)
    prov._served = {"d": v1}
    outs = prov.apply_many([(v1, a1, tmp_path / "o1.mp4"), (v2, a2, tmp_path / "o2.mp4")])
    assert all(o and o.exists() for o in outs)
    assert len(api.posts) == 2


def test_key_depends_on_model_and_output_url_shapes(tmp_path):
    v, a = _clip(tmp_path, "f", "red")
    p3, _ = _provider(tmp_path, FakeSyncSo())
    p2, _ = _provider(tmp_path, FakeSyncSo(), model="lipsync-2-pro")
    assert p3.key(v, a) != p2.key(v, a)
    assert _output_url({"outputUrl": "u1"}) == "u1"
    assert _output_url({"outputs": [{"url": "u2"}]}) == "u2"
    assert _output_url({"status": "COMPLETED"}) is None


def test_missing_generation_id_is_an_error(tmp_path):
    v, a = _clip(tmp_path, "g", "red")
    prov, _ = _provider(tmp_path, FakeSyncSo(no_id=True))
    with pytest.raises(SyncSoError, match="generation id"):
        prov.apply(v, a, tmp_path / "out.mp4")


def test_missing_key_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNC_API_KEY", raising=False)
    prov = SyncSoLipSync(tmp_path, {"lipsync": {"bucket": "b"}}, {}, SpendLedger(tmp_path / "c.jsonl", None))
    with pytest.raises(SyncSoError, match="SYNC_API_KEY"):
        prov.http()


def _marker(tmp_path, prov, v, a):
    return tmp_path / "cache" / "lipsync" / f"{prov.key(v, a)}.pending.json"


def test_lost_download_is_resumed_without_paying_twice(tmp_path, monkeypatch):
    v, a = _clip(tmp_path, "h", "red")
    api = FakeSyncSo(polls=1)
    good = SyncSoLipSync._download                              # the fixture's stand-in download

    def lost(self, url, out):
        raise SyncSoError("lip-sync: could not download the sync.so result from cdn.example (ConnectionError)")
    monkeypatch.setattr(SyncSoLipSync, "_download", lost)
    prov, ledger = _provider(tmp_path, api, budget=10.0)
    prov._served = {"h": v}
    with pytest.raises(SyncSoError, match="download"):
        prov.apply(v, a, tmp_path / "out.mp4")
    assert len(ledger.records()) == 1                            # the finished generation is paid for
    assert _marker(tmp_path, prov, v, a).exists() and prov.cached(v, a) is None
    # the next run polls that generation again instead of submitting (and paying for) a new one
    monkeypatch.setattr(SyncSoLipSync, "_download", good)
    prov2, ledger2 = _provider(tmp_path, api, budget=10.0)
    prov2._served = {"h": v}
    out = prov2.apply(v, a, tmp_path / "out2.mp4")
    assert out and out.exists() and len(api.posts) == 1
    assert len(ledger2.records()) == 1 and not _marker(tmp_path, prov, v, a).exists()


def test_timed_out_generation_is_picked_up_by_the_next_run(tmp_path):
    v, a = _clip(tmp_path, "i", "blue")
    api = FakeSyncSo(polls=2)
    prov, ledger = _provider(tmp_path, api, budget=10.0, timeout_seconds=-1)
    prov._served = {"i": v}
    with pytest.raises(SyncSoError, match="no result after"):
        prov.apply(v, a, tmp_path / "out.mp4")
    assert _marker(tmp_path, prov, v, a).exists() and ledger.records() == []
    prov2, ledger2 = _provider(tmp_path, api, budget=10.0)
    prov2._served = {"i": v}
    assert prov2.apply(v, a, tmp_path / "out2.mp4").exists()
    assert len(api.posts) == 1 and len(ledger2.records()) == 1


def test_pending_generation_that_is_gone_is_submitted_again(tmp_path):
    v, a = _clip(tmp_path, "j", "green")
    api = FakeSyncSo(polls=1)
    prov, ledger = _provider(tmp_path, api, budget=10.0)
    prov._served = {"j": v}
    marker = _marker(tmp_path, prov, v, a)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"generation": "gen-old", "usd": 0.27, "seconds": 2, "run": "r0", "charged": False}))
    out = prov.apply(v, a, tmp_path / "out.mp4")
    assert out and out.exists()
    assert api.gets[0].endswith("/gen-old") and len(api.posts) == 1   # asked for the old one, then made a new one
    assert len(ledger.records()) == 1 and not marker.exists()
