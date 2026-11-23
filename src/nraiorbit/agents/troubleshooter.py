"""The troubleshooter: reads why a film's run stopped (the stop, the film's plan, the runner's log, the owner's
words) and names the one thing to do about it, for the control room's Fix dialog (service/fix)."""

from __future__ import annotations

from ..schemas import StopDiagnosis
from .base import Agent, load_prompt


class Troubleshooter(Agent):
    name = "troubleshooter"
    references = ""                                    # it reads a stop, not a film's craft: no reference notes

    def __init__(self, llm, cfg: dict):
        super().__init__(llm, cfg)
        self.system = load_prompt(self.name)           # the house style is about writing films; this is not that

    def diagnose(self, evidence: str, words: str) -> StopDiagnosis:
        task = ("This film's run stopped. The evidence above is everything the control room knows about it.\n"
                + (f"The owner added:\n<owner_words>\n{words}\n</owner_words>\n" if words else
                   "The owner added nothing.\n")
                + "Say what happened and why, and propose the one remedy that gets this film going again. Use only "
                  "shot ids, steps and figures that appear in the evidence.")
        return self.ask(StopDiagnosis, context=evidence, task=task)
