"""The editor: reads the owner's rejection of the cut or the trailer and decides what to redo, and turns the owner's
wishes for the trailer into choices the trailer planner can honour."""

from __future__ import annotations

from ..schemas import TrailerDirection, TriageDecision
from .base import Agent


class Editor(Agent):
    name = "editor"

    def triage(self, context: str, gate: str, notes: str) -> TriageDecision:
        task = (f"The owner reviewed the film at the {gate} review and rejected it with these notes:\n"
                f"<owner_notes>\n{notes}\n</owner_notes>\n"
                "Decide what to redo. Use only the routes and ids in the context, the narrowest fix first, at most "
                "three actions. If the notes cannot be tied to anything in the film, act on nothing and ask one "
                "short question instead.")
        return self.ask(TriageDecision, context=context, task=task)

    def trailer_direction(self, context: str, notes: str) -> TrailerDirection:
        task = (f"The owner wants the trailer changed:\n<owner_notes>\n{notes}\n</owner_notes>\n"
                "Turn that into a trailer direction using only shot ids and line ids from the context. Leave a field "
                "empty (or 0, or same) when the notes say nothing about it.")
        return self.ask(TrailerDirection, context=context, task=task)
