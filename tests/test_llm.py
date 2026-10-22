import json
import threading
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from nraiorbit import schemas as S
from nraiorbit.llm import FALLBACK_BETA, ClaudeClient, LLMRefusal, LLMTruncated

LLM_FACING = [S.StoryBible, S.Outline, S.SceneScript, S.ShotList, S.Critique, S.CueRevisions, S.IdentityPackBrief,
              S.SaferShot, S.ReworkedShot]


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, message):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.message = message

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.message)


def _message(stop_reason="end_turn", parsed=None):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, usage=usage,
                           model="claude-opus-5-5", stop_details=None)


def test_request_shape_caching_fallback_structured_output(tmp_path):
    critique = S.Critique(score=9, issues=[], revision_instructions=[])
    fake = FakeClient(_message(parsed=critique))
    client = ClaudeClient(client=fake, usage_log=tmp_path / "usage.jsonl")
    out = client.generate(schema=S.Critique, system="SYS", context="BIBLE", task="TASK", effort="medium",
                          agent="dialogue_critic")
    assert out == critique
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is S.Critique
    assert call["output_config"] == {"effort": "medium"}
    assert call["thinking"] == {"type": "adaptive"}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = call["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "BIBLE", "cache_control": {"type": "ephemeral"}}
    assert content[1] == {"type": "text", "text": "TASK"}
    assert "dialogue_critic" in (tmp_path / "usage.jsonl").read_text()


def test_empty_context_sends_no_empty_block():
    fake = FakeClient(_message(parsed=S.Critique(score=1, issues=[], revision_instructions=[])))
    ClaudeClient(client=fake).generate(schema=S.Critique, system="S", context="", task="T")
    assert fake.calls[0]["messages"][0]["content"] == [{"type": "text", "text": "T"}]


@pytest.mark.parametrize("stop, exc", [("refusal", LLMRefusal), ("max_tokens", LLMTruncated)])
def test_stop_reasons_raise(stop, exc):
    client = ClaudeClient(client=FakeClient(_message(stop_reason=stop)))
    with pytest.raises(exc):
        client.generate(schema=S.Critique, system="S", context="", task="T")


@pytest.mark.parametrize("schema", LLM_FACING, ids=lambda s: s.__name__)
def test_schemas_are_strict_structured_output_compatible(schema):
    from anthropic.lib._parse._transform import transform_schema

    transformed = transform_schema(TypeAdapter(schema).json_schema())

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
                assert set(node["properties"]) <= set(node.get("required", []))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(transformed)


def test_client_is_created_lazily(monkeypatch):
    """Commands that never call Claude (POC run, shot pack, identity build) must work without the SDK."""
    import builtins

    real_import = builtins.__import__

    def no_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("anthropic not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    client = ClaudeClient()                      # must not import anthropic
    with pytest.raises(ImportError):
        _ = client.client                        # only the first real call needs it


def test_client_names_the_workspace_when_the_key_is_organization_level(monkeypatch):
    pytest.importorskip("anthropic")
    from nraiorbit.llm import ClaudeClient
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_0123")
    assert ClaudeClient().client.default_headers.get("anthropic-workspace-id") == "wrkspc_0123"
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    assert "anthropic-workspace-id" not in ClaudeClient().client.default_headers


def test_the_warm_start_loses_nothing_with_the_real_sdk(tmp_path, monkeypatch):
    """On ordered_map's warm thread ClaudeClient reads the first stream event before get_final_message, so the other
    calls start once the prompt cache is being written. With the real SDK over a mock transport (no network) that
    answers a 429 first, then a reply streamed once, an event at a time, the reply parses whole and its usage row is
    complete: the event read early (message_start, which carries the input and cache tokens) is not lost."""
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from nraiorbit import concurrency

    answer = S.Critique(score=7, issues=["a", "b" * 50], revision_instructions=["c" * 40, "d"])
    text = answer.model_dump_json()
    usage = {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 0}
    events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                    "content": [], "model": "claude-opus-5-5", "stop_reason": None,
                                                    "stop_sequence": None, "usage": usage}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
              {"type": "ping"},
              *({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 7]}}
                for i in range(0, len(text), 7)),
              {"type": "content_block_stop", "index": 0},
              {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
               "usage": {"output_tokens": 42}},
              {"type": "message_stop"}]

    def body():                                 # a one-shot stream, as from the network: it cannot be read twice
        for e in events:
            yield f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
    asked, lock = [], threading.Lock()

    def handler(request):
        with lock:
            asked.append(json.loads(request.content)["messages"][0]["content"][-1]["text"])
            first = len(asked) == 1
        if first:
            return httpx2.Response(429, headers={"retry-after-ms": "10"},
                                   json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})
        return httpx2.Response(200, content=body(), headers={
            "content-type": "text/event-stream", "anthropic-ratelimit-output-tokens-limit": "80000",
            "anthropic-ratelimit-output-tokens-remaining": "70000"})

    marks, mark = [], concurrency.mark_started

    def spy():                                  # who told the waiting calls to start, and whether anyone waited
        marks.append(concurrency.started_event() is not None)
        mark()
    monkeypatch.setattr(concurrency, "mark_started", spy)
    sdk = anthropic.Anthropic(api_key="test", base_url="http://test.local",
                              http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    client = ClaudeClient(client=sdk, usage_log=tmp_path / "usage.jsonl")
    out = list(concurrency.ordered_map(
        lambda task: client.generate(schema=S.Critique, system="S", context="C", task=task), ["t0", "t1"], 2))
    assert [(i, o.error, o.value) for i, o in out] == [(0, None, answer), (1, None, answer)]
    assert marks == [True]                      # t0 read its first event on the warm thread; t1 had no one waiting
    assert asked == ["t0", "t0", "t1"]          # the warm call's 429 and its retry; t1 only once t0 was streaming
    rows = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [(r["input_tokens"], r["cache_creation_input_tokens"], r["output_tokens"], r["rl_output_limit"],
             r["rl_output_remaining"]) for r in rows] == [(10, 5, 42, "80000", "70000")] * 2
