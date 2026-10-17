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


# Which parts of a reference profile (service.learn) each agent sees as examples, besides the summary and lessons.
REFERENCE_PARTS = {
    "story_analyst": ("emotion", "characters"),
    "screenwriter": ("emotion", "characters"),
    "casting": ("characters",),
    "director": ("action", "emotion"),
    "music_director": ("score",),
    "trailer": ("trailer",),
}


class Agent:
    name: str = "agent"
    prompt_name: str | None = None

    def __init__(self, llm: LLM, cfg: dict):
        self.llm = llm
        self.cfg = cfg
        self.effort = cfg.get("llm", {}).get("effort", {}).get(self.name, "high")
        self.system = load_prompt(SHARED_PROMPT) + "\n\n" + load_prompt(self.prompt_name or self.name)
        self.references = reference_examples(self.name, cfg)

    def ask(self, schema: type[T], *, context: str, task: str) -> T:
        if self.references:
            context = (context + "\n\n" if context else "") + self.references
        return self.llm.generate(
            schema=schema, system=self.system, context=context, task=task,
            effort=self.effort, agent=self.name,
        )


def reference_examples(agent_name: str, cfg: dict) -> str:
    """Craft notes learned from public films (library/references), as examples for this agent; '' when there are
    none or references.enabled is false. They are patterns to learn from, never material to copy."""
    r = cfg.get("references") or {}
    if r.get("enabled", True) is False:
        return ""
    from ..service.learn import examples_text, load_profiles
    profiles = load_profiles(Path(r["folder"]) if r.get("folder") else None, limit=int(r.get("limit", 3)))
    body = examples_text(profiles, parts=("summary", "lessons", *REFERENCE_PARTS.get(agent_name, ())))
    if not body:
        return ""
    return ("<references>\nCraft notes from films the studio studied. Learn the patterns; do not copy lines, "
            "likenesses or melodies (see each profile's cautions).\n" + body + "\n</references>")


def project_context(*parts: tuple[str, BaseModel | str | None]) -> str:
    """Stable, cacheable context block: titled JSON sections in a fixed order."""
    chunks = []
    for title, value in parts:
        if value is None:
            continue
        body = value.model_dump_json(indent=1) if isinstance(value, BaseModel) else value
        chunks.append(f"<{title}>\n{body}\n</{title}>")
    return "\n\n".join(chunks)
