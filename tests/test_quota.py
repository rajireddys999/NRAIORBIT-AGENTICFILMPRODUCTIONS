"""Google's quota used up mid-film: the image call waits it out, a run that still meets it goes back to retry (the
next tick goes on from what was made), and the third such run in a row stops with a plain reason."""

import pytest

from nraiorbit.costing import SpendLedger
from nraiorbit.providers import google_ai
from nraiorbit.providers.google_ai import GeminiImages, GoogleAPIError, quota_used_up
from nraiorbit.service import queue

QUOTA = GoogleAPIError("Gemini image (keyframe sc009_s001): 429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}")


def images(tmp_path, monkeypatch, failures, error=QUOTA):
    calls, slept = [], []

    def call(fn, what, **kw):
        calls.append(what)
        if len(calls) <= failures:
            raise error
        return "response"
    monkeypatch.setattr(google_ai, "_call", call)
    monkeypatch.setattr(google_ai, "_types", lambda: type("T", (), {"GenerateContentConfig": dict, "ImageConfig": dict}))
    monkeypatch.setattr(google_ai, "_first_inline", lambda response: (b"png", "image/png"))
    monkeypatch.setattr(google_ai, "SLEEP", slept.append)
    ledger = SpendLedger(tmp_path / "cost.jsonl", 10.0)
    from types import SimpleNamespace
    client = SimpleNamespace(models=SimpleNamespace(generate_content=None))      # _call is faked: never called
    gen = GeminiImages(client, {}, {"gemini": {"image_per_image": 0.04}}, ledger)
    return gen, ledger, calls, slept


def test_a_used_up_quota_is_waited_out_and_never_paid_for(tmp_path, monkeypatch):
    gen, ledger, calls, slept = images(tmp_path, monkeypatch, failures=2)
    out = gen.generate("a frame", tmp_path / "f.png", item="keyframe sc009_s001")
    assert out.read_bytes() == b"png" and len(calls) == 3 and slept == [60, 120]
    assert ledger.spent() == pytest.approx(0.04)                             # one image paid, not three


def test_a_quota_that_never_comes_back_is_reported_and_other_errors_are_not_waited_for(tmp_path, monkeypatch):
    gen, ledger, calls, slept = images(tmp_path, monkeypatch, failures=10)
    with pytest.raises(GoogleAPIError, match="RESOURCE_EXHAUSTED"):
        gen.generate("a frame", tmp_path / "f.png")
    assert slept == [60, 120, 240] and ledger.spent() == 0
    gen, ledger, calls, slept = images(tmp_path, monkeypatch, failures=1,
                                       error=GoogleAPIError("Gemini image (x): 400 INVALID_ARGUMENT"))
    with pytest.raises(GoogleAPIError, match="INVALID_ARGUMENT"):
        gen.generate("a frame", tmp_path / "g.png")
    assert slept == [] and len(calls) == 1
    assert quota_used_up(QUOTA) and not quota_used_up(GoogleAPIError("model not found"))
    for never in ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests, limit: 0",
                  "429 RESOURCE_EXHAUSTED. Your prepayment credits are depleted"):
        assert not quota_used_up(GoogleAPIError(never))                       # never refills: stop with its fix
    assert not quota_used_up(GoogleAPIError("400 INVALID_ARGUMENT: payload 4294967296 bytes"))   # 429 inside a number
    assert quota_used_up(GoogleAPIError("Gemini image (x): 429 Too Many Requests"))


def test_a_run_google_quota_stopped_goes_back_to_retry_twice_then_stops_plainly():
    row = {"slug": "film", "state": "producing", "note": ""}
    for expected_state, strikes in (("retry", 1), ("retry", 2), ("blocked:quota", 3)):
        row.update(queue.after_quota(row))
        assert row["state"] == expected_state and row["note"].startswith(f"quota ({strikes}):")
    assert "everything already made is kept" in row["note"].lower()
    assert queue.quota_strikes("restore failed (2): x") == 0


def test_the_tick_turns_a_quota_error_into_a_retry(tmp_path, monkeypatch):
    import csv
    import io
    stories, refs = tmp_path / "stories.csv", tmp_path / "references.csv"
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    template = tmp_path / "projects" / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    queue.save(str(stories), [{"slug": "rain", "story_uri": str(tmp_path / "rain.md"), "budget_usd": "40",
                               "gates": "ask", "publish": "none", "state": "new", "note": "", "updated": ""}],
               queue.STORY_COLUMNS)
    queue.save(str(refs), [], queue.REFERENCE_COLUMNS)

    def produce_fn(root, bucket):
        raise QUOTA
    for expected in ("retry", "retry", "blocked:quota"):
        queue.tick(str(stories), str(refs), tmp_path / "projects", bucket=None, produce_fn=produce_fn, learn_fn=None,
                   template=template, log=lambda *_: None)
        row = next(csv.DictReader(io.StringIO(stories.read_text())))
        assert row["state"] == expected, row
