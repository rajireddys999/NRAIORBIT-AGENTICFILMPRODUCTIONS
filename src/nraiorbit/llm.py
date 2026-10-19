"""LLM access for the agents.

`ClaudeClient` calls the Anthropic API with structured outputs (the agent's Pydantic
schema), adaptive thinking, streaming, prompt caching on the stable prefix
(system prompt + project context) and server-side refusal fallback.

`ReplayLLM` returns recorded fixtures. It powers tests and the offline `--demo` run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel

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
    ):
        self._client = client      # created on first use, so runs that never call Claude don't need the SDK
        self.model = model
        self.max_tokens = max_tokens
        self.usage_log = usage_log

    @property
    def client(self):
        if self._client is None:
            import os

            import anthropic

            # An organization-level key (one not made inside a workspace) must name the workspace on every request;
            # ANTHROPIC_WORKSPACE_ID (wrkspc_...) does that. A workspace-scoped key needs nothing.
            workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
            self._client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace} if workspace else None)
        return self._client

    def generate(self, *, schema, system, context, task, effort="high", agent="agent"):
        content: list[dict] = []
        if context:
            content.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": task})

        started = time.time()
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            output_format=schema,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            message = stream.get_final_message()

        self._log_usage(agent, schema.__name__, message, time.time() - started)

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusal(f"{agent}: request declined ({details}). Rephrase the scene in cinematic terms.")
        if message.stop_reason == "max_tokens":
            raise LLMTruncated(f"{agent}: output hit max_tokens={self.max_tokens}; split the task smaller.")
        parsed = message.parsed_output
        if parsed is None:
            raise LLMError(f"{agent}: no structured output returned (stop_reason={message.stop_reason})")
        return parsed

    def _log_usage(self, agent: str, schema_name: str, message: Any, seconds: float) -> None:
        if self.usage_log is None:
            return
        usage = message.usage
        record = {
            "ts": time.time(),
            "agent": agent,
            "schema": schema_name,
            "model": message.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            "seconds": round(seconds, 2),
            "request_id": getattr(message, "_request_id", None),
        }
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.usage_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


Fixture = dict | Callable[[str], dict]


class ReplayLLM:
    """Returns fixtures keyed by schema class name.

    A fixture is either a dict or a callable receiving the task text (so per-scene
    fixtures can key off the scene id mentioned in the task).
    """

    def __init__(self, fixtures: dict[str, Fixture]):
        self.fixtures = fixtures
        self.calls: list[tuple[str, str]] = []

    def generate(self, *, schema, system, context, task, effort="high", agent="agent"):
        name = schema.__name__
        self.calls.append((agent, name))
        if name not in self.fixtures:
            raise LLMError(f"ReplayLLM has no fixture for {name} (agent={agent})")
        fixture = self.fixtures[name]
        data = fixture(task) if callable(fixture) else fixture
        return schema.model_validate(data)
