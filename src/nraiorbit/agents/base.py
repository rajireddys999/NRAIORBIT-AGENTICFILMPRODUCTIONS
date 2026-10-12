"""Agent base: a named role with a versioned system prompt and a per-agent effort level."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from ..llm import LLM

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SHARED_PROMPT = "shared_house_style"

T = TypeVar("T", bound=BaseModel)


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


class Agent:
    name: str = "agent"
    prompt_name: str | None = None

    def __init__(self, llm: LLM, cfg: dict):
        self.llm = llm
        self.cfg = cfg
        self.effort = cfg.get("llm", {}).get("effort", {}).get(self.name, "high")
        self.system = load_prompt(SHARED_PROMPT) + "\n\n" + load_prompt(self.prompt_name or self.name)

    def ask(self, schema: type[T], *, context: str, task: str) -> T:
        return self.llm.generate(
            schema=schema, system=self.system, context=context, task=task,
            effort=self.effort, agent=self.name,
        )


def project_context(*parts: tuple[str, BaseModel | str | None]) -> str:
    """Stable, cacheable context block: titled JSON sections in a fixed order."""
    chunks = []
    for title, value in parts:
        if value is None:
            continue
        body = value.model_dump_json(indent=1) if isinstance(value, BaseModel) else value
        chunks.append(f"<{title}>\n{body}\n</{title}>")
    return "\n\n".join(chunks)
