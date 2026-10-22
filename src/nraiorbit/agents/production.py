"""Casting, Director and Music Director agents."""

from __future__ import annotations

from ..library.registry import AssetRegistry
from ..schemas import (
    CastEntry, CastLock, CharacterSpec, CueRevisions, CueSheet, IdentityPackBrief,
    ReworkedShot, SaferShot, SceneOutline, SceneScript, Shot, ShotList, StoryBible,
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


def most_shots(target_seconds: float, clip_lengths: list[int]) -> int:
    """How many shots a scene of this length holds when every shot is one clip of at least the shortest length."""
    return max(1, int(target_seconds) // max(1, min(clip_lengths)))


def least_shots(target_seconds: float, clip_lengths: list[int]) -> int:
    """How many shots a scene of this length needs when every shot is one clip of at most the longest length."""
    longest = max(1, max(clip_lengths))
    return min(most_shots(target_seconds, clip_lengths), max(1, -(-int(target_seconds) // longest)))


def shots_fit(n: int, target_seconds: float, clip_lengths: list[int]) -> bool:
    return least_shots(target_seconds, clip_lengths) <= n <= most_shots(target_seconds, clip_lengths)


def shot_budget(target_seconds: float, clip_lengths: list[int]) -> str:
    lengths = ", ".join(str(n) for n in sorted(set(clip_lengths)))
    return (f"Every shot is generated as ONE video clip of exactly {lengths} seconds, and the whole clip is paid for: "
            f"a shorter shot pays for seconds that are cut away. So plan between {least_shots(target_seconds, clip_lengths)} "
            f"and {most_shots(target_seconds, clip_lengths)} shots, each duration_s one of {lengths}, adding up to "
            f"{int(target_seconds)} s. Let one shot carry several "
            "dialogue lines (block_ids) and cover reactions within a shot, rather than cutting on every line.")


class Director(Agent):
    name = "director"

    def shots(self, bible: StoryBible, lock: CastLock, scene: SceneOutline, script: SceneScript,
              action_candidates: list[str], rasa_defaults: dict, clip_lengths: list[int] | None = None,
              issue: str = "", off_screen: dict[str, str] | None = None) -> ShotList:
        task = (
            f"Plan the shots for {scene.scene_id}. Target scene length: {scene.target_seconds} s.\n"
            f"<scene_outline>\n{scene.model_dump_json(indent=1)}\n</scene_outline>\n"
            f"<scene_script>\n{script.model_dump_json(indent=1)}\n</scene_script>\n"
            f"<rasa_camera_defaults>\n{rasa_defaults}\n</rasa_camera_defaults>\n"
            f"<action_templates>\n{chr(10).join(action_candidates) or 'none'}\n</action_templates>"
        )
        if clip_lengths:
            task += "\n" + shot_budget(scene.target_seconds, clip_lengths)
        if off_screen:
            task += "\n" + off_screen_rule(off_screen)
        if issue:
            task += f"\nYour previous shot list for this scene could not be used: {issue}"
        context = project_context(("story_bible", bible), ("cast_lock", lock))
        return self.ask(ShotList, context=context, task=task)

    def rework_shot(self, bible: StoryBible, lock: CastLock, shot: Shot, notes: str,
                    off_screen: dict[str, str] | None = None) -> ReworkedShot:
        """The owner sent this shot back: the same moment of the scene, planned again as the notes ask."""
        task = (f"The owner watched the film and sent shot {shot.shot_id} back for rework.\n"
                f"<shot>\n{shot.model_dump_json(indent=1)}\n</shot>\n"
                f"<owner_notes>\n{notes}\n</owner_notes>\n"
                "Plan this shot again as the notes ask: who is in frame (character ids from the cast), their costume "
                "ids in the same order, the keyframe description and what moves. Keep its place in the scene, its "
                "length and its camera unless the notes say otherwise.")
        if off_screen:
            task += "\n" + off_screen_rule(off_screen)
        return self.ask(ReworkedShot, context=project_context(("story_bible", bible), ("cast_lock", lock)), task=task)

    def safer_shot(self, bible: StoryBible, shot: Shot, refusal: str) -> SaferShot:
        """Google's image or video model refused this shot for safety: the same story moment, worded so it passes."""
        task = (
            f"Google's image or video model refused shot {shot.shot_id} for safety ({refusal[:300]}).\n"
            f"<shot>\n{shot.model_dump_json(indent=1)}\n</shot>\n"
            "Rewrite its image_prompt and motion_prompt so the shot shows the same story moment, the same characters "
            "(keep their ids), place, light, mood and camera, but without what such filters block: no weapon raised "
            "at, pointed at or striking a person; no wounds, blood, bruises or injuries; nobody falling, hanging or "
            "dangling over a drop in visible peril; no fire on or touching a person; no screaming in pain; no knife "
            "or blade near anyone; no impact words (hit, smash, strike, slam into); no child in danger or a child's "
            "point of view; no blood-like stains; no written words, numbers or logos in the frame; when the refusal "
            "is about a child or person/face generation, take the child out of the frame entirely. Carry the "
            "danger through faces, framing, light and setting instead: the threat can be implied or just off frame. "
            "Keep it a strong, cinematic frame for a family-friendly feature film."
        )
        return self.ask(SaferShot, context=project_context(("story_bible", bible)), task=task)


def off_screen_rule(off_screen: dict[str, str]) -> str:
    """The planning rule for people the project's video model must not show (characters.minors_off_screen)."""
    return (f"This project's video model must not show anyone under 18: {', '.join(off_screen.values())}. Keep them "
            "out of every shot: never in characters_in_frame, never described in image_prompt or motion_prompt. "
            "Their lines are heard off screen, over the other characters' reactions or the place.")


class MusicDirector(Agent):
    name = "music_director"

    def revise(self, bible: StoryBible, sheet: CueSheet, motifs: list[str]) -> CueRevisions:
        task = (
            "Review the planned cue sheet and propose revisions only where they improve the film.\n"
            f"<cue_sheet>\n{sheet.model_dump_json(indent=1)}\n</cue_sheet>\n"
            f"<available_motifs>\n{chr(10).join(motifs) or 'none'}\n</available_motifs>"
        )
        return self.ask(CueRevisions, context=project_context(("story_bible", bible)), task=task)
