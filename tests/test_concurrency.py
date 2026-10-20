"""Independent Claude calls at the same time (concurrency.py): same calls, same files, same logs as one at a time."""

import copy
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit import concurrency
from nraiorbit import schemas as S
from nraiorbit.agents import production
from nraiorbit.concurrency import ordered_map
from nraiorbit.config import REPO_ROOT, deep_merge, load_pipeline_config
from nraiorbit.demo import DEMO_CONFIG, DEMO_PLOT, demo_llm
from nraiorbit.llm import ClaudeClient, LLMError, ReplayLLM
from nraiorbit.pipeline import GatePending, Pipeline

# ---------------------------------------------------------------- ordered_map


def _timed(record, lock, seconds=0.2, mark=False, fail=()):
    def fn(x):
        start = time.monotonic()
        if mark:
            concurrency.mark_started()
        if x in fail:
            with lock:
                record[x] = (start, time.monotonic())
            raise ValueError(f"item {x}")
        time.sleep(seconds)
        with lock:
            record[x] = (start, time.monotonic())
        return x * 10
    return fn


def _overlapping(spans) -> int:
    """How many pairs of (start, end) spans run at the same time."""
    spans = list(spans)
    return sum(1 for i, a in enumerate(spans) for b in spans[i + 1:] if a[0] < b[1] and b[0] < a[1])


def test_results_come_in_input_order_and_overlap():
    record, lock = {}, threading.Lock()
    out = list(ordered_map(_timed(record, lock), list(range(6)), 3))
    assert [i for i, _ in out] == list(range(6))
    assert [o.value for _, o in out] == [i * 10 for i in range(6)] and all(o.error is None for _, o in out)
    assert _overlapping(record.values()) >= 2


def test_warm_first_waits_for_the_first_stream_event():
    record, lock = {}, threading.Lock()
    list(ordered_map(_timed(record, lock), list(range(4)), 3))        # no started signal: item 0 runs alone
    assert all(record[0][1] <= record[i][0] for i in range(1, 4))
    record.clear()
    list(ordered_map(_timed(record, lock, mark=True), list(range(4)), 3))   # streaming at once: the others start
    assert record[1][0] < record[0][1]


def test_one_worker_stops_at_the_first_error():
    record, lock = {}, threading.Lock()
    out = list(ordered_map(_timed(record, lock, seconds=0, fail={2}), list(range(5)), 1))
    assert [i for i, _ in out] == [0, 1, 2] and isinstance(out[2][1].error, ValueError)
    assert set(record) == {0, 1, 2}


def test_a_failing_warm_item_starts_nothing_else():
    record, lock = {}, threading.Lock()
    out = list(ordered_map(_timed(record, lock, fail={0}), list(range(5)), 3))
    assert [i for i, _ in out] == [0] and isinstance(out[0][1].error, ValueError)
    assert set(record) == {0}


def test_after_an_error_nothing_new_starts_and_finished_items_are_yielded():
    record, lock = {}, threading.Lock()
    out = list(ordered_map(_timed(record, lock, fail={2}), list(range(8)), 4))
    started = sorted(record)
    assert started == list(range(len(started))) and len(started) < 8      # a prefix, never all of them
    assert [i for i, _ in out] == started                                  # every started item is yielded, in order
    assert isinstance(dict(out)[2].error, ValueError)
    assert all(dict(out)[i].value == i * 10 for i in started if i != 2)


def test_workers_knob_is_clamped():
    assert concurrency.workers({}) == 1
    assert concurrency.workers({"llm": {"concurrency": 0}}) == 1
    assert concurrency.workers({"llm": {"concurrency": 3}}) == 3
    assert concurrency.workers({"llm": {"concurrency": 99}}) == 8


# ---------------------------------------------------------------- the planning stages


def _sid(task: str) -> str:
    return re.search(r"sc\d{3}", task).group(0)


def _fixtures() -> dict:
    """The demo's recorded answers, with a draft synopsis that differs from the outline's, a critic that passes only
    revised scenes (so the revise and re-critique path runs) and a brief for every character."""
    fixtures = dict(demo_llm().fixtures)
    scene = fixtures["SceneScript"]

    def script(task):
        data = copy.deepcopy(scene(task))
        sid = data["scene_id"]
        data["synopsis"] = f"written {sid}" + (" (revised)" if "<critique>" in task else "")
        return data

    fixtures["SceneScript"] = script
    fixtures["Critique"] = lambda task: {"score": 9 if "(revised)" in task else 6, "issues": ["flat"],
                                         "revision_instructions": ["sharpen"]}
    fixtures["IdentityPackBrief"] = lambda task: S.IdentityPackBrief(
        character_id=(cid := re.search(r"fits '([^']+)'", task).group(1)), visual_brief=f"{cid}, synthetic",
        face_prompts=["front, neutral"], body_prompts=["full body"], costume_prompts=["baseline"],
        negative_prompt="no real person", library_tags=[cid]).model_dump()
    return fixtures


class SlowLLM:
    """ReplayLLM answers after a short sleep, recording every call and when it ran. `fail(schema, task)` true makes
    the call fail; a string it returns is the error's message."""

    def __init__(self, fixtures, seconds=0.2, fail=None):
        self.inner = ReplayLLM(fixtures)
        self.seconds, self.fail = seconds, fail
        self.lock = threading.Lock()
        self.calls: list[tuple] = []

    def generate(self, *, schema, system, context, task, effort="high", agent="agent"):
        start = time.monotonic()
        why = self.fail and self.fail(schema.__name__, task)
        if why:
            with self.lock:
                self.calls.append((agent, schema.__name__, task, context, start, time.monotonic()))
            raise LLMError(why if isinstance(why, str) else "429 rate limited")
        time.sleep(self.seconds)
        out = self.inner.generate(schema=schema, system=system, context=context, task=task, effort=effort,
                                  agent=agent)
        with self.lock:
            self.calls.append((agent, schema.__name__, task, context, start, time.monotonic()))
        return out

    def spans(self, schema: str):
        return [(c[4], c[5]) for c in self.calls if c[1] == schema]


@pytest.fixture(autouse=True)
def _brief_everyone(monkeypatch):
    monkeypatch.setattr(production, "CAST_MATCH_THRESHOLD", 1.01)    # no library match: every character is briefed


def _project(tmp_path: Path, name: str, workers: int, gates: dict | None = None):
    root = tmp_path / name
    (root / "input").mkdir(parents=True)
    (root / "input" / "story.md").write_text(DEMO_PLOT, encoding="utf-8")
    cfg = deep_merge(load_pipeline_config(), copy.deepcopy(DEMO_CONFIG))
    cfg["llm"]["concurrency"] = workers
    if gates is not None:
        cfg["review_gates"] = gates
    return root, cfg


def _run(tmp_path, workers, **llm_kw):
    root, cfg = _project(tmp_path, f"w{workers}", workers)
    llm, lines = SlowLLM(_fixtures(), **llm_kw), []
    Pipeline(root, llm, cfg=cfg, log=lines.append).run(until="shots")
    return root, llm, lines


def _files(root: Path) -> dict[str, bytes]:
    paths = [root / "01_story_bible.json", root / "02_outline.json", root / "cast.lock.yaml"]
    for folder in ("03_scenes", "04_shots", "04_identity_briefs"):
        paths += sorted((root / folder).glob("*"))
    paths += [p for p in sorted((root / "reports").glob("*.jsonl")) if p.name != "llm_usage.jsonl"]
    return {str(p.relative_to(root)): p.read_bytes() for p in paths}


def test_concurrent_planning_writes_what_the_sequential_run_writes(tmp_path):
    base_root, base_llm, base_lines = _run(tmp_path, 1)
    base_files = _files(base_root)
    assert "reports/critiques.jsonl" in base_files and len(list((base_root / "04_shots").glob("*"))) == 6
    assert len(list((base_root / "04_identity_briefs").glob("*"))) == 3
    tasks = [c[2] for c in base_llm.calls if c[1] == "SceneScript"]
    assert any("Previous scene ended with: written sc001 (revised)" in t for t in tasks)   # the chain reads the scene
    base_calls = Counter(c[:4] for c in base_llm.calls)
    for workers in (2, 4):
        root, llm, lines = _run(tmp_path, workers)
        assert _files(root) == base_files
        assert Counter(c[:4] for c in llm.calls) == base_calls
        assert lines == base_lines
        assert _overlapping(llm.spans("ShotList")) >= 1
        assert _overlapping(llm.spans("IdentityPackBrief")) == 0           # briefs stay one at a time


def _cid(task: str) -> str:
    return re.search(r"fits '([^']+)'", task).group(1)


def test_a_failing_brief_stops_the_casting_before_a_later_brief_is_paid(tmp_path):
    """No brief is saved until every one is written, so a brief run beside a failing one would be paid for twice:
    once thrown away, once on the retry. One at a time, the retry pays what the one-at-a-time loop always paid."""
    root, cfg = _project(tmp_path, "cast", 2)
    Pipeline(root, SlowLLM(_fixtures(), seconds=0), cfg=cfg, log=lambda m: None).run(until="scenes")
    on = {"fail": True}
    llm = SlowLLM(_fixtures(), seconds=0.05,
                  fail=lambda schema, task: on["fail"] and schema == "IdentityPackBrief" and _cid(task) == "meera")
    with pytest.raises(LLMError, match="429"):
        Pipeline(root, llm, cfg=cfg, log=lambda m: None).run(until="cast")
    assert [_cid(c[2]) for c in llm.calls if c[1] == "IdentityPackBrief"] == ["arjun", "meera"]   # bhairava waits
    on["fail"], n = False, len(llm.calls)
    Pipeline(root, llm, cfg=cfg, log=lambda m: None).run(until="cast")
    assert [_cid(c[2]) for c in llm.calls[n:] if c[1] == "IdentityPackBrief"] == ["arjun", "meera", "bhairava"]
    assert sorted(p.stem for p in (root / "04_identity_briefs").glob("*.json")) == ["arjun", "bhairava", "meera"]


def test_template_gates_cast_once(tmp_path):
    template = yaml.safe_load((REPO_ROOT / "projects" / "_template" / "pipeline.yaml").read_text(encoding="utf-8"))
    for workers in (1, 2):
        root, cfg = _project(tmp_path, f"gates{workers}", workers, gates=template["review_gates"])
        llm, lines, seen = SlowLLM(_fixtures(), seconds=0.05), [], []
        while True:
            try:
                Pipeline(root, llm, cfg=cfg, log=lines.append).run(until="shots")
                break
            except GatePending as g:
                seen.append(g.gate)
                if g.gate == "shots":
                    break
                (root / "reviews" / f"{g.gate}.approved").write_text("ok", encoding="utf-8")
        assert "scenes" in seen and "cast" in seen and seen[-1] == "shots"
        for cid in ("arjun", "meera", "bhairava"):
            assert sum(line.startswith(f"  {cid} -> ") for line in lines) == 1
        assert len(list((root / "04_shots").glob("*.json"))) == 6


def test_a_failing_shot_list_keeps_the_ones_before_and_starts_nothing_new(tmp_path):
    root, cfg = _project(tmp_path, "fail", 2)
    llm = SlowLLM(_fixtures(), seconds=0.05, fail=lambda schema, task: schema == "ShotList" and _sid(task) == "sc003")
    with pytest.raises(LLMError, match="429"):
        Pipeline(root, llm, cfg=cfg, log=lambda m: None).run(until="shots")
    called = [_sid(c[2]) for c in llm.calls if c[1] == "ShotList"]
    saved = sorted(p.stem for p in (root / "04_shots").glob("*.json"))
    assert sorted(called) == ["sc001", "sc002", "sc003"]      # sc003 fails at once while sc002 runs: nothing new
    assert saved == ["sc001", "sc002"]


def test_a_later_shot_list_running_when_an_earlier_one_fails_is_saved_and_the_earlier_error_is_raised(tmp_path):
    """sc004 fails at once and sc002 a moment later, both while sc003 runs: sc003 is saved anyway, so the retry never
    pays for it again, and the error raised is sc002's, the first in scene order, as the one-at-a-time loop's."""
    root, cfg = _project(tmp_path, "later", 3)
    Pipeline(root, SlowLLM(_fixtures(), seconds=0), cfg=cfg, log=lambda m: None).run(until="cast")
    on = {"fail": True}

    def fail(schema, task):
        if not on["fail"] or schema != "ShotList" or _sid(task) not in ("sc002", "sc004"):
            return False
        if _sid(task) == "sc002":
            time.sleep(0.05)
        return f"{_sid(task)}: 429 rate limited"
    llm = SlowLLM(_fixtures(), seconds=0.2, fail=fail)
    with pytest.raises(LLMError, match="^sc002: "):
        Pipeline(root, llm, cfg=cfg, log=lambda m: None).run(until="shots")
    assert sorted(p.stem for p in (root / "04_shots").glob("*.json")) == ["sc001", "sc003"]
    on["fail"], n = False, len(llm.calls)
    Pipeline(root, llm, cfg=cfg, log=lambda m: None).run(until="shots")
    assert sorted(_sid(c[2]) for c in llm.calls[n:] if c[1] == "ShotList") == ["sc002", "sc004", "sc005", "sc006"]
    assert sorted(p.stem for p in (root / "04_shots").glob("*.json")) == [f"sc00{i}" for i in range(1, 7)]


def test_a_missing_scene_file_plans_the_scenes_before_it(tmp_path):
    root, cfg = _project(tmp_path, "missing", 2)
    pipe = Pipeline(root, SlowLLM(_fixtures(), seconds=0.01), cfg=cfg, log=lambda m: None)
    pipe.run(until="cast")
    (root / "03_scenes" / "sc004.json").unlink()
    with pytest.raises(FileNotFoundError):
        pipe._shots()
    assert sorted(p.stem for p in (root / "04_shots").glob("*.json")) == ["sc001", "sc002", "sc003"]


# ---------------------------------------------------------------- ClaudeClient under threads


HEADERS = {"anthropic-ratelimit-output-tokens-limit": "80000", "anthropic-ratelimit-output-tokens-remaining": "64000"}


class FakeStream:
    def __init__(self, message, fail=None):
        self.message, self.fail = message, fail
        self.response = SimpleNamespace(headers=HEADERS)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="message_start")

    def get_final_message(self):
        if self.fail is not None:
            raise self.fail
        time.sleep(0.05)
        return self.message


class FakeClient:
    def __init__(self, message, fail_task=None, error=None):
        self.message, self.fail_task, self.error = message, fail_task, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        task = kwargs["messages"][0]["content"][-1]["text"]
        return FakeStream(self.message, self.error if task == self.fail_task else None)


def _message():
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason="end_turn", parsed_output=S.Critique(score=9, issues=[], revision_instructions=[]),
                           usage=usage, model="claude-opus-5-5", stop_details=None)


def test_a_rate_limit_among_concurrent_calls_surfaces_and_the_others_return(tmp_path, monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    import httpx
    error = anthropic.RateLimitError("rate limited", response=httpx.Response(
        429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return FakeClient(_message(), fail_task="t2", error=error)

    monkeypatch.setattr(anthropic, "Anthropic", factory)
    client = ClaudeClient(usage_log=tmp_path / "usage.jsonl")
    out = dict(ordered_map(lambda t: client.generate(schema=S.Critique, system="S", context="C", task=t),
                           ["t0", "t1", "t2", "t3"], 4))
    assert len(made) == 1 and made[0]["max_retries"] == 6
    assert out[2].error is error
    assert [out[i].value.score for i in (0, 1, 3)] == [9, 9, 9]


def test_usage_rows_from_many_threads(tmp_path):
    log = tmp_path / "usage.jsonl"
    client = ClaudeClient(client=FakeClient(_message()), usage_log=log)
    threads = [threading.Thread(target=client.generate, kwargs=dict(schema=S.Critique, system="S", context="C",
                                                                    task=f"t{i}", agent="director"))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(rows) == 8 and all(r["rl_output_limit"] == "80000" and r["rl_output_remaining"] == "64000"
                                  for r in rows)
