"""Story Analyst, Screenwriter and Dialogue Critic."""

from __future__ import annotations

from ..schemas import Critique, Outline, SceneOutline, SceneScript, StoryBible
from .base import Agent, project_context


class StoryAnalyst(Agent):
    name = "story_analyst"

    def run(self, plot: str, runtime_minutes: float, template: str, template_guide: str) -> StoryBible:
        template_line = (
            "Choose the structure_template that best fits the plot."
            if template == "auto" else f"Use structure_template '{template}'."
        )
        task = (
            f"Build the Story Bible for a {runtime_minutes:g}-minute Telugu feature film.\n"
            f"{template_line}\n\n{template_guide}\n\n<plot>\n{plot}\n</plot>"
        )
        return self.ask(StoryBible, context="", task=task)


class Screenwriter(Agent):
    name = "screenwriter"

    def outline(self, bible: StoryBible, runtime_minutes: float, template_guide: str,
                issues: list[str] | None = None) -> Outline:
        task = (
            f"Write the scene outline for the whole film. Scene target_seconds must sum to "
            f"{int(runtime_minutes * 60)} seconds.\n{template_guide}"
        )
        if issues:
            task += "\n\nThe previous outline had these problems; fix them:\n- " + "\n- ".join(issues)
        return self.ask(Outline, context=project_context(("story_bible", bible)), task=task)

    def scene(self, bible: StoryBible, outline: Outline, scene: SceneOutline,
              previous_summary: str | None) -> SceneScript:
        task = (
            f"Write the full scene script for {scene.scene_id}.\n"
            f"<scene_outline>\n{scene.model_dump_json(indent=1)}\n</scene_outline>\n"
            f"Previous scene ended with: {previous_summary or 'this is the first scene'}"
        ) + self._animals_rule(bible)
        return self.ask(SceneScript, context=self._context(bible, outline), task=task)

    def revise(self, bible: StoryBible, outline: Outline, script: SceneScript, critique: Critique) -> SceneScript:
        task = (
            f"Revise scene {script.scene_id} using the critique. Keep block ids stable where the "
            f"block survives.\n<scene_script>\n{script.model_dump_json(indent=1)}\n</scene_script>\n"
            f"<critique>\n{critique.model_dump_json(indent=1)}\n</critique>"
        ) + self._animals_rule(bible)
        return self.ask(SceneScript, context=self._context(bible, outline), task=task)

    def _animals_rule(self, bible: StoryBible) -> str:
        """Animals make sounds, not speeches: a line given to one would be read aloud by a voice and lip-synced onto
        its face. voice.animals_speak (true, or a list of ids) is for a story whose animals talk."""
        from ..characters import silent_animals
        animals = list(silent_animals(bible.characters, self.cfg).values())
        if not animals:
            return ""
        return (f"\nThese characters are animals and never speak: {', '.join(animals)}. Write their sounds and "
                "behaviour as action lines (kind action, no character_id), never as dialogue.")

    @staticmethod
    def _context(bible: StoryBible, outline: Outline) -> str:
        return project_context(("story_bible", bible), ("outline", outline))


class DialogueCritic(Agent):
    name = "dialogue_critic"

    def review(self, bible: StoryBible, script: SceneScript) -> Critique:
        task = f"Review this scene.\n<scene_script>\n{script.model_dump_json(indent=1)}\n</scene_script>"
        return self.ask(Critique, context=project_context(("story_bible", bible)), task=task)
