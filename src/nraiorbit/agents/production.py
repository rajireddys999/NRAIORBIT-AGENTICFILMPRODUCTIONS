"""Casting, Director and Music Director agents."""

from __future__ import annotations

from ..library.registry import AssetRegistry
from ..schemas import (
    CastEntry, CastLock, CharacterSpec, CueRevisions, CueSheet, IdentityPackBrief,
    SceneOutline, SceneScript, ShotList, StoryBible,
)
from .base import Agent, project_context

CAST_MATCH_THRESHOLD = 0.3   # >= 60% of role/gender/age/dialect tags


def _character_query_tags(spec: CharacterSpec) -> list[str]:
    decade = f"{(spec.physical.age // 10) * 10}s"
    return [spec.role, spec.physical.gender.lower(), decade, spec.voice.dialect]


class CastingAgent(Agent):
    """Library-first casting. Unmatched characters get an identity-pack brief.

    Briefs are written one at a time: none is saved until every one is written, so a brief running beside one that
    fails would be paid for, thrown away and paid for again on the retry.
    """

    name = "casting"

    def cast(self, bible: StoryBible, library: AssetRegistry, production_only: bool = False
             ) -> tuple[CastLock, list[IdentityPackBrief]]:
        entries: dict[str, CastEntry] = {}
        briefs: list[IdentityPackBrief] = []
        taken: set[str] = set()
        for spec in bible.characters:
            hits = [h for h in library.search("character", tags=_character_query_tags(spec),
                                              production_only=production_only, k=10)
                    if h.asset.id not in taken and getattr(h.asset, "role", None) in (spec.role, None)]
            if hits and hits[0].score >= CAST_MATCH_THRESHOLD:
                asset = hits[0].asset
                taken.add(asset.id)
                voice = asset.default_voice
                motif = next((m.asset for m in library.search("motif", tags=[spec.role, asset.id], k=1)), None)
                entries[spec.id] = CastEntry(
                    character_asset=library.ref(asset),
                    voice_asset=library.ref(library.get("voice", voice)) if voice else None,
                    leitmotif_asset=library.ref(motif) if motif else None,
                    costumes={c.id: c.description for c in asset.costumes},
                )
            else:
                briefs.append(self.brief(bible, spec))
                entries[spec.id] = CastEntry(character_asset=f"characters/{spec.id}@pending")
        return CastLock(characters=entries), briefs

    def brief(self, bible: StoryBible, spec: CharacterSpec) -> IdentityPackBrief:
        task = (
            f"No library character fits '{spec.id}'. Write the identity-pack brief for a new, "
            f"entirely synthetic person.\n<character>\n{spec.model_dump_json(indent=1)}\n</character>"
        )
        return self.ask(IdentityPackBrief, context=project_context(("story_bible", bible)), task=task)


class Director(Agent):
    name = "director"

    def shots(self, bible: StoryBible, lock: CastLock, scene: SceneOutline, script: SceneScript,
              action_candidates: list[str], rasa_defaults: dict) -> ShotList:
        task = (
            f"Plan the shots for {scene.scene_id}. Target scene length: {scene.target_seconds} s.\n"
            f"<scene_outline>\n{scene.model_dump_json(indent=1)}\n</scene_outline>\n"
            f"<scene_script>\n{script.model_dump_json(indent=1)}\n</scene_script>\n"
            f"<rasa_camera_defaults>\n{rasa_defaults}\n</rasa_camera_defaults>\n"
            f"<action_templates>\n{chr(10).join(action_candidates) or 'none'}\n</action_templates>"
        )
        context = project_context(("story_bible", bible), ("cast_lock", lock))
        return self.ask(ShotList, context=context, task=task)


class MusicDirector(Agent):
    name = "music_director"

    def revise(self, bible: StoryBible, sheet: CueSheet, motifs: list[str]) -> CueRevisions:
        task = (
            "Review the planned cue sheet and propose revisions only where they improve the film.\n"
            f"<cue_sheet>\n{sheet.model_dump_json(indent=1)}\n</cue_sheet>\n"
            f"<available_motifs>\n{chr(10).join(motifs) or 'none'}\n</available_motifs>"
        )
        return self.ask(CueRevisions, context=project_context(("story_bible", bible)), task=task)
