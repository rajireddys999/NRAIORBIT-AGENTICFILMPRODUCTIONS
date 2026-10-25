"""The cost guardrails: the lip-sync estimate in the preview, the produce cost gate, publish only on a passing
film check, Lyria under --offline, YouTube without a browser, and the learn cost guard."""

import json
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.costing import BudgetExceeded, SpendLedger, load_pricing, preview_run, speaking_shots
from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
from nraiorbit.pipeline import Pipeline
from nraiorbit.poc import create_poc
from nraiorbit.post import ffmpeg
from nraiorbit.service import produce

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")


# ------------------------------------------------------------------------------------- 1. estimate and cost gate

def test_preview_counts_sync_so_seconds_for_shots_with_a_speaker_in_frame(tmp_path):
    root = create_poc(tmp_path / "poc")
    base = load_pipeline_config(root)
    pricing = load_pricing(root)
    plain = preview_run(root, base, pricing)
    assert plain["lipsync_usd"] == 0.0 and "lipsync_usd" in plain
    cfg = deep_merge(base, {"providers": {"lipsync": "sync_so"}, "lipsync": {"model": "sync-3"}})
    pre = preview_run(root, cfg, pricing)
    speaking = speaking_shots(root)
    assert speaking and pre["lipsync_seconds"] == pytest.approx(sum(speaking.values()), abs=0.1)
    assert pre["lipsync_usd"] == pytest.approx(pre["lipsync_seconds"] * 0.133, abs=0.01)
    assert pre["total_usd"] == pytest.approx(plain["total_usd"] + pre["lipsync_usd"], abs=0.01)
    # cheaper model, cheaper estimate; offline: nothing new is paid
    cheap = preview_run(root, deep_merge(cfg, {"lipsync": {"model": "lipsync-2"}}), pricing)
    assert cheap["lipsync_usd"] == pytest.approx(pre["lipsync_seconds"] * 0.04, abs=0.01)
    assert preview_run(root, deep_merge(cfg, {"lipsync": {"offline": True}}), pricing)["lipsync_usd"] == 0.0
    # a shot with a synced clip on disk is not counted again (a re-run after a stop)
    first = sorted(speaking)[0]
    (root / "07_video" / first).mkdir(parents=True, exist_ok=True)
    (root / "07_video" / first / "take_1_lipsync.mp4").write_bytes(b"")
    again = preview_run(root, cfg, pricing)
    assert again["cached_lipsync_shots"] == 1
    assert again["lipsync_seconds"] == pytest.approx(pre["lipsync_seconds"] - speaking[first], abs=0.1)
    # only shots whose own dialogue speaker is in frame count: a shot with no character in frame never does
    from nraiorbit.schemas import ShotList
    for path in sorted((root / "04_shots").glob("*.json")):
        sl = ShotList.model_validate_json(path.read_text(encoding="utf-8"))
        for s in sl.shots:
            if not s.characters_in_frame:
                assert s.shot_id not in speaking


def _demo_pipeline(tmp_path, **extra):
    root = tmp_path / "demo"
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = deep_merge(DEMO_CONFIG, {"policy": {"gates": "auto", "publish": "none"}, **extra})
    (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return Pipeline(root, demo_llm(), cfg=load_pipeline_config(root), log=lambda *_: None)


@needs_ffmpeg
def test_produce_stops_before_paying_when_the_estimate_passes_the_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    from nraiorbit.providers import lyria as lyria_mod

    def never(*a, **k):
        raise AssertionError("a paid Lyria call was attempted")
    monkeypatch.setattr(lyria_mod.LyriaMusic, "_generate", never)
    monkeypatch.setattr(lyria_mod.LyriaMusic, "client", property(never), raising=False)
    # Lyria is a paid generator; the demo's video and voices are mock. No cap at all: blocked before any call.
    p = _demo_pipeline(tmp_path, providers={"music": "lyria"}, budget={"max_usd": 0})
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "blocked:no cap" and report["steps"]["plan"] == "done"
    assert report["steps"]["estimate"]["music_usd"] > 0 and "budget.max_usd" in report["steps"]["render"]
    assert not (p.p.root / "07_video").exists() and not list((p.p.root / "08_music").glob("*.wav"))
    assert SpendLedger(p.p.reports / "cost.jsonl", None).spent() == 0.0
    # a cap below the estimate: blocked:estimate, with the numbers in the reason
    p.cfg["budget"] = {"max_usd": 0.10}
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "blocked:estimate" and "over the cap $0.10" in report["steps"]["render"]
    # a cap with room: the gate passes (the mock run then goes on; music would be paid, so stop it here)
    p.cfg["budget"] = {"max_usd": 50.0}
    block, preview = produce.cost_gate(p)
    assert block is None and preview["total_usd"] > 0
    # free providers: no estimate, no gate
    p.cfg["providers"]["music"] = "mock"
    assert produce.cost_gate(p) == (None, None)


@needs_ffmpeg
def test_produce_holds_publish_for_the_owner_and_says_when_the_film_check_failed(tmp_path, monkeypatch):
    """The YouTube upload waits for the owner under every policy, auto too; a failed film check is said at the gate,
    and the owner's approval uploads once at the policy's privacy."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    from nraiorbit.qa import film
    monkeypatch.setattr(film, "check", lambda root, log=print: {"ok": False, "why": "a 90 ms lag"})
    uploads = []
    monkeypatch.setattr(produce, "publish_youtube", lambda root, privacy, bucket=None: uploads.append(privacy) or {"film": "x"})
    p = _demo_pipeline(tmp_path, policy={"gates": "auto", "publish": "private"})
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "awaiting:publish" and report["steps"]["qa_film"] == "look" and uploads == []
    assert "The film check did not pass: look at QA first." in (p.p.reviews / "publish.pending").read_text()
    produce.approve(p, "publish", "approved in the control room\n")
    report = produce.produce(p, log=lambda *_: None)
    assert report["state"] == "done" and report["steps"]["publish"] == {"film": "x"} and uploads == ["private"]


# ------------------------------------------------------------------------------------- 2. Lyria under --offline

def test_offline_flag_covers_music_as_well_as_lipsync():
    from nraiorbit import cli
    cfg = cli._apply_run_options({"lipsync": {}, "music": {}}, SimpleNamespace(offline=True))
    assert cfg["lipsync"]["offline"] is True and cfg["music"]["offline"] is True
    cfg = cli._apply_run_options({"lipsync": {}}, SimpleNamespace(offline=False))
    assert "offline" not in cfg["lipsync"] and "offline" not in cfg.get("music", {})


@needs_ffmpeg
def test_offline_lyria_uses_the_cache_and_never_calls(tmp_path):
    pytest.importorskip("google.genai")
    from test_lyria import FakeLyria, cue, lyria_cfg, provider
    root = create_poc(tmp_path / "poc")
    fake = FakeLyria(text_only=True)
    cfg = deep_merge(lyria_cfg(root), {"music": {"offline": True}})
    from nraiorbit.providers.lyria import LyriaMusic
    from nraiorbit.config import load_navarasa
    music = LyriaMusic(root, cfg, load_pricing(root), SpendLedger(root / "reports" / "cost.jsonl", None),
                       load_navarasa(), client=fake)
    with pytest.raises(BudgetExceeded, match="offline: Lyria would be paid for cue002"):
        music.render_cue(cue(), root / "08_music" / "cue002.wav")
    assert fake.calls == []
    # once a cue is generated (online), offline serves it from the cache
    online, _ = provider(root, FakeLyria(tmp_path))
    online.render_cue(cue(), root / "08_music" / "cue002.wav")
    assert music.render_cue(cue(), root / "08_music" / "cue002b.wav").exists() and fake.calls == []


# ------------------------------------------------------------------------------------- 3. YouTube without a browser

def test_youtube_credentials_never_open_a_browser_on_a_server(tmp_path, monkeypatch):
    pytest.importorskip("google.oauth2.credentials")
    from nraiorbit.publish import youtube
    token = tmp_path / ".youtube_token.json"
    monkeypatch.delenv("YOUTUBE_TOKEN_JSON", raising=False)
    with pytest.raises(RuntimeError, match="release youtube <slug> --auth"):
        youtube.credentials(tmp_path / "client_secret.json", token, interactive=False)
    assert not token.exists()
    # the token secret seeds the file; a token that has not expired is used as it stands (no refresh call)
    seed = {"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s", "scopes": youtube.SCOPES,
            "type": "authorized_user", "expiry": "2099-01-01T00:00:00Z"}
    monkeypatch.setenv("YOUTUBE_TOKEN_JSON", json.dumps(seed))
    creds = youtube.credentials(tmp_path / "client_secret.json", token, interactive=False)
    assert creds.valid and json.loads(token.read_text())["refresh_token"] == "r"


# ------------------------------------------------------------------------------------- 4. learn cost guard

def test_learn_refuses_an_expensive_watch_unless_told_yes(tmp_path):
    pytest.importorskip("google.genai")
    from nraiorbit.service import learn
    from test_service import FakeGemini
    pricing = load_pricing(None)
    est, tokens = learn.estimate_usd(150, pricing=pricing)
    assert est > 1.0 and tokens > 200_000
    short, _ = learn.estimate_usd(10, pricing=pricing)
    assert short < 0.2
    fake = FakeGemini()
    with pytest.raises(learn.LearnCostError, match=r"length not known, priced as 150 min.* over the \$1.00 limit"):
        learn.learn("https://www.youtube.com/watch?v=abc123xyz", cfg={"google": {}}, client=fake,
                    out_dir=tmp_path / "refs", pricing=pricing)
    assert fake.calls == []
    # a known short length passes; the watch is written to the library's ledger with its real token counts
    out = learn.learn("https://www.youtube.com/watch?v=abc123xyz", cfg={"google": {}}, client=fake,
                      out_dir=tmp_path / "refs", pricing=pricing, minutes=12)
    assert out.exists() and len(fake.calls) == 1
    row = json.loads((tmp_path / "refs" / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["provider"] == "gemini_video" and row["tokens_in"] == 12000
    assert row["usd"] == pytest.approx(12000 / 1e6 * 1.25 + 900 / 1e6 * 10, abs=1e-4)
    # --yes overrides the limit
    learn.learn("https://www.youtube.com/watch?v=abc123xyz", cfg={"google": {}}, client=fake,
                out_dir=tmp_path / "refs", pricing=pricing, yes=True)
    assert len(fake.calls) == 2
