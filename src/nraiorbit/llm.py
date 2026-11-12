"""LLM access for the agents.

`ClaudeClient` calls the Anthropic API with structured outputs (the agent's Pydantic
schema), adaptive thinking, streaming, prompt caching on the stable prefix
(system prompt + project context) and server-side refusal fallback.

`ReplayLLM` returns recorded fixtures. It powers tests and the offline `--demo` run.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel

from . import concurrency

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class LLMTruncated(LLMError):
    pass


class LLM(Protocol):
    def generate(
        self,
        *,
        schema: type[T],
        system: str,
        context: str,
        task: str,
        effort: str = "high",
        agent: str = "agent",
        model: str | None = None,
    ) -> T: ...


class ClaudeClient:
    """Structured-output Claude client used by every agent.

    `context` is the stable, per-project prefix (Story Bible, outline, lockfile). It is
    cached, so repeated per-scene calls only pay full price for `task`.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 64000,
        usage_log: Path | None = None,
        client: Any | None = None,
        batch: dict | None = None,
    ):
        self._client = client      # created on first use, so runs that never call Claude don't need the SDK
        self.model = model
        self.max_tokens = max_tokens
        self.usage_log = usage_log
        self._lock = threading.Lock()   # shot lists may call from several threads (concurrency.py)
        # llm.batch: the agents whose calls go to Claude as one Message Batch at half price when several arrive
        # together (the shot lists, one per scene). A batch is slower than a direct call (minutes, up to an hour),
        # which a background runner can afford; a stuck batch is cancelled and its calls run one by one.
        self.batch = dict(batch or {})
        self.batch_agents = frozenset(self.batch.get("agents") or ())
        self._window: _BatchWindow | None = None

    @property
    def client(self):
        with self._lock:
            if self._client is None:
                import os

                import anthropic

                # An organization-level key (one not made inside a workspace) must name the workspace on every
                # request; ANTHROPIC_WORKSPACE_ID (wrkspc_...) does that. A workspace-scoped key needs nothing.
                # max_retries=6: concurrent calls meet rate limits more often; the SDK backs off and retries them.
                workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
                self._client = anthropic.Anthropic(
                    default_headers={"anthropic-workspace-id": workspace} if workspace else None, max_retries=6)
            return self._client

    def generate(self, *, schema, system, context, task, effort="high", agent="agent", model=None):
        """`model`: this call's model (llm.models per agent), else the client's."""
        if agent in self.batch_agents:
            return self._generate_batched(schema=schema, system=system, context=context, task=task, effort=effort,
                                          agent=agent, model=model)
        return self._generate_stream(schema=schema, system=system, context=context, task=task, effort=effort,
                                     agent=agent, model=model)

    def _request(self, *, system, context, task, model) -> dict:
        """The parts of a request that are the same streamed or batched: the cached system prompt and context."""
        content: list[dict] = []
        if context:
            content.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": task})
        return {"model": model or self.model, "max_tokens": self.max_tokens,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": content}], "thinking": {"type": "adaptive"}}

    def _generate_stream(self, *, schema, system, context, task, effort, agent, model):
        started = time.time()
        with self.client.beta.messages.stream(
            **self._request(system=system, context=context, task=task, model=model),
            output_config={"effort": effort},
            output_format=schema,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            if concurrency.started_event() is not None:
                # the first stream event means the prompt cache this call writes is readable: let the others start
                next(iter(stream), None)
                concurrency.mark_started()
            message = stream.get_final_message()
            headers = getattr(getattr(stream, "response", None), "headers", None) or {}

        self._log_usage(agent, schema.__name__, message, time.time() - started,
                        rl_output_limit=headers.get("anthropic-ratelimit-output-tokens-limit"),
                        rl_output_remaining=headers.get("anthropic-ratelimit-output-tokens-remaining"))
        self._check_stop(message, agent)
        parsed = message.parsed_output
        if parsed is None:
            raise LLMError(f"{agent}: no structured output returned (stop_reason={message.stop_reason})")
        return parsed

    def _check_stop(self, message: Any, agent: str) -> None:
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusal(f"{agent}: request declined ({details}). Rephrase the scene in cinematic terms.")
        if message.stop_reason == "max_tokens":
            raise LLMTruncated(f"{agent}: output hit max_tokens={self.max_tokens}; split the task smaller.")

    # -- the Message Batches API: half price for calls that can wait

    def _generate_batched(self, *, schema, system, context, task, effort, agent, model):
        """This call joins the batch window with the others of its kind and waits for its answer. Nothing to warm
        for the callers behind it (a batch writes and reads its own cache), so they are let go at once."""
        from pydantic import TypeAdapter

        from anthropic.lib._parse._transform import transform_schema

        concurrency.mark_started()
        params = self._request(system=system, context=context, task=task, model=model)
        params["output_config"] = {"effort": effort, "format": {
            "type": "json_schema", "schema": transform_schema(TypeAdapter(schema).json_schema())}}
        slot = self._batch_window().submit(params, agent, schema.__name__)
        slot.done.wait()
        if slot.fallback:                                # too few to batch, a stuck batch, or a request that errored
            return self._generate_stream(schema=schema, system=system, context=context, task=task, effort=effort,
                                         agent=agent, model=model)
        message = slot.message
        self._log_usage(agent, schema.__name__, message, slot.seconds, batch=True)
        self._check_stop(message, agent)
        text = "".join(getattr(b, "text", "") for b in (message.content or []) if getattr(b, "type", "") == "text")
        if not text.strip():
            raise LLMError(f"{agent}: no structured output returned (stop_reason={message.stop_reason})")
        return schema.model_validate_json(text)

    def _batch_window(self) -> "_BatchWindow":
        with self._lock:
            if self._window is None:
                b = self.batch
                self._window = _BatchWindow(lambda: self.client, quiet_s=float(b.get("quiet_seconds", 3)),
                                            min_calls=int(b.get("min_calls", 2)),
                                            wait_s=60 * float(b.get("wait_minutes", 60)),
                                            poll_s=float(b.get("poll_seconds", 20)))
            return self._window

    def _log_usage(self, agent: str, schema_name: str, message: Any, seconds: float, *,
                   rl_output_limit: str | None = None, rl_output_remaining: str | None = None,
                   batch: bool = False) -> None:
        if self.usage_log is None:
            return
        usage = message.usage
        record = {
            "ts": time.time(),
            "agent": agent,
            "schema": schema_name,
            "model": message.model,
            "batch": batch,                               # a Message Batch call: billed at half the rates
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            "seconds": round(seconds, 2),
            "request_id": getattr(message, "_request_id", None),
            # the output-token rate limit and what was left, so llm.concurrency can be raised on evidence
            "rl_output_limit": rl_output_limit,
            "rl_output_remaining": rl_output_remaining,
        }
        with self._lock:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")


class _Slot:
    """One caller's place in a batch window: its request, then its answer (or the word to run it directly)."""

    def __init__(self, params: dict, agent: str, schema_name: str):
        self.params, self.agent, self.schema_name = params, agent, schema_name
        self.done = threading.Event()
        self.message: Any = None
        self.fallback = False
        self.seconds = 0.0
        self.submitted = time.time()


class _BatchWindow:
    """Calls that arrive within `quiet_s` of each other go to Claude as one Message Batch (half price); each
    caller's thread waits for its own answer. Fewer than `min_calls` in a window run one by one as usual, and a
    batch that has not ended after `wait_s` is cancelled and its callers run one by one, so a film is never stuck
    behind the queue. A request the batch could not serve (errored, expired, cancelled) also runs one by one."""

    def __init__(self, client: Callable[[], Any], *, quiet_s: float, min_calls: int, wait_s: float, poll_s: float):
        self._client, self.quiet_s, self.min_calls, self.wait_s, self.poll_s = client, quiet_s, min_calls, wait_s, poll_s
        self._lock = threading.Lock()
        self._pending: list[_Slot] = []
        self._timer: threading.Timer | None = None
        self.batches: list[str] = []                     # ids, for the log and the tests

    def submit(self, params: dict, agent: str, schema_name: str) -> _Slot:
        slot = _Slot(params, agent, schema_name)
        with self._lock:
            self._pending.append(slot)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.quiet_s, self._fire)
            self._timer.daemon = True
            self._timer.start()
        return slot

    def _fire(self) -> None:
        with self._lock:
            slots, self._pending, self._timer = self._pending, [], None
        if not slots:
            return
        if len(slots) < self.min_calls:
            for s in slots:
                s.fallback = True
                s.done.set()
            return
        threading.Thread(target=self._run, args=(slots,), daemon=True).start()

    def _run(self, slots: list[_Slot]) -> None:
        batch_id = None
        try:
            client = self._client()
            batch = client.beta.messages.batches.create(requests=[
                {"custom_id": str(i), "params": s.params} for i, s in enumerate(slots)])
            batch_id = batch.id
            self.batches.append(batch_id)
            deadline = time.time() + self.wait_s
            while getattr(batch, "processing_status", "") != "ended":
                if time.time() > deadline:
                    try:
                        client.beta.messages.batches.cancel(batch_id)
                    except Exception:
                        pass
                    raise TimeoutError(f"batch {batch_id} not ended after {self.wait_s / 60:.0f} min")
                time.sleep(self.poll_s)
                batch = client.beta.messages.batches.retrieve(batch_id)
            answered = {}
            for r in client.beta.messages.batches.results(batch_id):
                result = r.result
                if getattr(result, "type", "") == "succeeded":
                    answered[r.custom_id] = result.message
            for i, s in enumerate(slots):
                message = answered.get(str(i))
                if message is None:
                    s.fallback = True
                else:
                    s.message, s.seconds = message, round(time.time() - s.submitted, 2)
        except Exception:                                # the API, the network, the deadline: every caller runs direct
            for s in slots:
                s.fallback = True
        finally:
            for s in slots:
                s.done.set()


Fixture = dict | Callable[[str], dict]


class ReplayLLM:
    """Returns fixtures keyed by schema class name.

    A fixture is either a dict or a callable receiving the task text (so per-scene
    fixtures can key off the scene id mentioned in the task).
    """

    def __init__(self, fixtures: dict[str, Fixture]):
        self.fixtures = fixtures
        self.calls: list[tuple[str, str]] = []
        self.models: list[tuple[str, str | None]] = []   # (agent, the model it asked for; None = the default)
        self._lock = threading.Lock()

    def generate(self, *, schema, system, context, task, effort="high", agent="agent", model=None):
        name = schema.__name__
        with self._lock:
            self.calls.append((agent, name))
            self.models.append((agent, model))
        if name not in self.fixtures:
            raise LLMError(f"ReplayLLM has no fixture for {name} (agent={agent})")
        fixture = self.fixtures[name]
        data = fixture(task) if callable(fixture) else fixture
        return schema.model_validate(data)
