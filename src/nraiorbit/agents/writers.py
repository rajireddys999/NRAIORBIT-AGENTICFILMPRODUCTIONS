"""Story Analyst, Screenwriter and Dialogue Critic."""

from __future__ import annotations

from ..schemas import Critique, Outline, SceneOutline, SceneScript, ShortenedLines, StoryBible
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
        ) + self._speech_budget(scene.target_seconds) + self._animals_rule(bible)
        return self.ask(SceneScript, context=self._context(bible, outline), task=task)

    def shorten_lines(self, bible: StoryBible, outline: Outline, script: SceneScript,
                      limits: dict[str, int]) -> ShortenedLines:
        """Lines that run past the shots carrying them, written shorter: {block_id: most Telugu letters}."""
        asked = "\n".join(f"- {bid}: at most {n} Telugu letters (spaces not counted)" for bid, n in limits.items())
        task = (f"These dialogue lines of scene {script.scene_id} are too long for the shots that carry them: a "
                "voice that runs on plays over the wrong faces. Write each one shorter, in spoken Telugu, keeping "
                "its speaker, meaning, emotion and its place in the scene; the English subtitle says the same. Keep "
                "each block_id; return only these lines. A line that cannot be said in that space may be dropped "
                "(text_te and text_en empty) when the scene still makes sense without it: better than one word.\n"
                f"{asked}\n"
                f"<scene_script>\n{script.model_dump_json(indent=1)}\n</scene_script>")
        return self.ask(ShortenedLines, context=self._context(bible, outline), task=task)

    @staticmethod
    def _speech_budget(seconds: float) -> str:
        """How much talking a scene of `seconds` holds (planning/fit): every line must play over its own shot."""
        from ..planning.fit import DIALOGUE_GAP_S, DIALOGUE_LEAD_IN_S, SCENE_SPEECH_SHARE, chars_for
        budget = seconds * SCENE_SPEECH_SHARE * 0.9 - 3 * (DIALOGUE_LEAD_IN_S + DIALOGUE_GAP_S)   # about 3 lines
        return (f"\nThis scene runs about {seconds} s on screen. Keep its dialogue to about {budget:.0f} s spoken "
                f"in all (about {chars_for(budget)} Telugu letters across every line), so each line plays over its "
                "own shot: fewer, shorter lines rather than speeches.")

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
