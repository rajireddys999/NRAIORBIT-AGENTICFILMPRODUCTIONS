"""The Message Batches API for calls that can wait (llm.batch): calls of the batch agents that arrive together go to
Claude as one batch at half price, each caller gets its own answer, too few calls or a stuck batch run one by one,
the usage log marks batch rows and the cost model halves them. Everything against a fake client: no real API."""

import json
import threading
from types import SimpleNamespace

import pytest

from nraiorbit import schemas as S
from nraiorbit.costing import claude_actual, load_pricing
from nraiorbit.llm import ClaudeClient


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def _usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _streamed(parsed):
    return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=_usage(), model="claude-sonnet-5-5",
                           stop_details=None, content=[])


def _batched(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, usage=_usage(), model="claude-sonnet-5-5", stop_details=None,
                           content=[SimpleNamespace(type="text", text=text)])


class FakeBatches:
    """Creates batches, reports them ended after `polls` polls, answers each request with `answer(params)`."""

    def __init__(self, answer, polls=1, never_ends=False):
        self.answer, self.polls, self.never_ends = answer, polls, never_ends
        self.created, self.cancelled, self.retrieved = [], [], 0

    def create(self, *, requests, betas=None):
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}", processing_status="in_progress")

    def retrieve(self, batch_id):
        self.retrieved += 1
        ended = not self.never_ends and self.retrieved >= self.polls
        return SimpleNamespace(id=batch_id, processing_status="ended" if ended else "in_progress")

    def results(self, batch_id):
        requests = self.created[int(batch_id.rsplit("_", 1)[-1]) - 1]
        for r in requests:
            yield SimpleNamespace(custom_id=r["custom_id"], result=self.answer(r["params"]))

    def cancel(self, batch_id):
        self.cancelled.append(batch_id)
        return SimpleNamespace(id=batch_id, processing_status="canceling")


class FakeClient:
    def __init__(self, streamed, batches):
        self.streams = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream, batches=batches))
        self.streamed = streamed

    def _stream(self, **kwargs):
        self.streams.append(kwargs)
        return FakeStream(self.streamed)


CRITIQUE = S.Critique(score=8, issues=["one"], revision_instructions=["two"])


def _client(tmp_path, batches, **batch_cfg):
    cfg = {"agents": ["director"], "quiet_seconds": 0.05, "min_calls": 2, "wait_minutes": 0.01, "poll_seconds": 0.01}
    cfg.update(batch_cfg)
    fake = FakeClient(_streamed(CRITIQUE), batches)
    return fake, ClaudeClient(client=fake, usage_log=tmp_path / "reports" / "llm_usage.jsonl", batch=cfg)


def _call(client, agent, task, out):
    out.append(client.generate(schema=S.Critique, system="SYS", context="BIBLE", task=task, effort="medium",
                               agent=agent, model="claude-sonnet-5-5"))


def test_calls_that_arrive_together_go_as_one_batch_at_half_price(tmp_path):
    def answer(params):
        return SimpleNamespace(type="succeeded", message=_batched(CRITIQUE.model_dump_json()))
    batches = FakeBatches(answer)
    fake, client = _client(tmp_path, batches)
    out = []
    threads = [threading.Thread(target=_call, args=(client, "director", f"scene {i}", out)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(out) == 3 and all(o == CRITIQUE for o in out)
    assert len(batches.created) == 1 and len(batches.created[0]) == 3 and fake.streams == []
    params = batches.created[0][0]["params"]
    assert params["model"] == "claude-sonnet-5-5" and params["thinking"] == {"type": "adaptive"}
    assert params["output_config"]["effort"] == "medium" and params["output_config"]["format"]["type"] == "json_schema"
    assert "score" in json.dumps(params["output_config"]["format"]["schema"])
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    rows = [json.loads(line) for line in (tmp_path / "reports" / "llm_usage.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and all(r["batch"] is True and r["agent"] == "director" for r in rows)
    pricing = load_pricing()
    usd, calls = claude_actual(tmp_path, pricing)
    assert calls == 3 and usd == pytest.approx(3 * 0.5 * (1000 * 2.0 + 100 * 10.0) / 1e6)   # Sonnet, half price


def test_other_agents_and_a_lone_call_stream_as_before(tmp_path):
    batches = FakeBatches(lambda p: SimpleNamespace(type="succeeded", message=_batched(CRITIQUE.model_dump_json())))
    fake, client = _client(tmp_path, batches)
    out = []
    _call(client, "screenwriter", "scene 1", out)                     # not a batch agent: streamed at once
    assert out == [CRITIQUE] and len(fake.streams) == 1 and batches.created == []
    _call(client, "director", "scene 1", out)                         # alone in its window: streamed after the wait
    assert out[-1] == CRITIQUE and len(fake.streams) == 2 and batches.created == []
    rows = [json.loads(line) for line in (tmp_path / "reports" / "llm_usage.jsonl").read_text().splitlines()]
    assert [r["batch"] for r in rows] == [False, False]


def test_a_stuck_batch_is_cancelled_and_its_calls_run_one_by_one(tmp_path):
    batches = FakeBatches(lambda p: None, never_ends=True)
    fake, client = _client(tmp_path, batches)
    out = []
    threads = [threading.Thread(target=_call, args=(client, "director", f"scene {i}", out)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(out) == 2 and all(o == CRITIQUE for o in out)
    assert batches.cancelled == ["msgbatch_1"] and len(fake.streams) == 2        # cancelled, then direct calls


def test_a_request_the_batch_could_not_serve_runs_direct_and_a_refusal_is_raised(tmp_path):
    def answer(params):
        task = params["messages"][0]["content"][-1]["text"]
        if task == "scene 0":
            return SimpleNamespace(type="errored", error=SimpleNamespace(type="api_error"))
        if task == "scene 1":
            return SimpleNamespace(type="succeeded", message=_batched("", stop_reason="refusal"))
        return SimpleNamespace(type="succeeded", message=_batched(CRITIQUE.model_dump_json()))
    batches = FakeBatches(answer)
    fake, client = _client(tmp_path, batches)
    out, errors = [], []

    def call(i):
        try:
            _call(client, "director", f"scene {i}", out)
        except Exception as e:
            errors.append((i, type(e).__name__))
    threads = [threading.Thread(target=call, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(out) == 2 and len(fake.streams) == 1                   # scene 0 ran direct; scene 2 from the batch
    assert errors == [(1, "LLMRefusal")]


def test_the_shot_lists_fan_out_all_at_once_when_the_director_is_batched():
    from nraiorbit.pipeline import fan_out

    def pipeline(llm, concurrency):
        return SimpleNamespace(director=SimpleNamespace(llm=llm), cfg={"llm": {"concurrency": concurrency}})
    batched = pipeline(SimpleNamespace(batch_agents=frozenset({"director"})), 2)
    plain = pipeline(SimpleNamespace(batch_agents=frozenset()), 2)
    replay = pipeline(object(), 3)
    assert fan_out(batched, 23) == 23 and fan_out(batched, 0) == 1
    assert fan_out(plain, 23) == 2 and fan_out(replay, 23) == 3
    assert fan_out(SimpleNamespace(director=SimpleNamespace(), cfg={}), 5) == 1    # a test's stand-in pipeline
