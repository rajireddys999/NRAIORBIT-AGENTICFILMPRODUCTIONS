"""Trailer cutter: score shots, fill a Telugu trailer template, avoid spoilers.

Template (2:15 by default): world -> hero -> conflict -> emotion -> escalation montage ->
punch dialogue -> title card -> stinger. Section shares scale to any target length.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schemas import Shot, StoryBible

SECTIONS: list[tuple[str, float, float]] = [
    # (section, share of trailer, max seconds per clip)
    ("world", 0.12, 2.5),
    ("hero", 0.15, 2.5),
    ("conflict", 0.15, 2.0),
    ("emotion", 0.14, 2.5),
    ("escalation", 0.22, 1.0),
    ("punch", 0.12, 5.0),
    ("title", 0.05, 0.0),
    ("stinger", 0.05, 3.0),
]
# Sections pick shots in this order (scarce material first), then play in SECTIONS order.
SELECTION_ORDER = ["punch", "world", "hero", "conflict", "emotion", "stinger", "escalation"]
SPOILER_CUTOFF = 0.88        # nothing from the final 12% of the film
CLIMAX_REGION = 0.80         # shots after this point appear only as escalation-montage glimpses
CLIMAX_GLIMPSE_MAX_S = 0.8


@dataclass
class TrailerClip:
    section: str
    shot_id: str | None      # None = title card
    film_start_s: float
    duration_s: float
    score: float


def _section_fits(section: str, shot: Shot, roles: dict[str, str]) -> bool:
    chars_roles = {roles.get(c) for c in shot.characters_in_frame}
    if section == "world":
        return shot.size in ("EWS", "WS", "aerial") and shot.intensity <= 6
    if section == "hero":
        return "hero" in chars_roles and shot.rasa in ("veera", "raudra", "adbhuta")
    if section == "conflict":
        return "villain" in chars_roles or shot.rasa in ("raudra", "bhayanaka")
    if section == "emotion":
        return shot.rasa in ("karuna", "shringara", "vatsalya")
    if section == "escalation":
        return shot.intensity >= 7
    if section == "punch":
        return shot.punch_dialogue
    if section == "stinger":
        return shot.rasa == "hasya" or shot.punch_dialogue
    return False


def score_shot(shot: Shot, roles: dict[str, str]) -> float:
    lead = any(roles.get(c) in ("hero", "heroine", "villain") for c in shot.characters_in_frame)
    size_bonus = 1.0 if shot.size in ("CU", "ECU", "MCU", "EWS") else 0.5
    return round(
        0.35 * shot.intensity / 10 + 0.25 * shot.trailer_candidate + 0.15 * lead
        + 0.15 * shot.punch_dialogue + 0.10 * size_bonus, 4)


def plan_trailer(shots: list[tuple[Shot, float]], bible: StoryBible, film_seconds: float,
                 target_seconds: float) -> list[TrailerClip]:
    """shots: (shot, film_start_s) for every shot in the film, in order."""
    roles = {c.id: c.role for c in bible.characters}
    shares = {name: (share, max_clip) for name, share, max_clip in SECTIONS}
    used: set[str] = set()
    picked: dict[str, list[TrailerClip]] = {}
    for section in SELECTION_ORDER:
        share, max_clip = shares[section]
        budget = share * target_seconds
        candidates = []
        for shot, start in shots:
            pos = start / film_seconds
            if shot.shot_id in used or pos > SPOILER_CUTOFF or not _section_fits(section, shot, roles):
                continue
            if pos > CLIMAX_REGION and section != "escalation":
                continue
            clip_max = CLIMAX_GLIMPSE_MAX_S if pos > CLIMAX_REGION else max_clip
            candidates.append((score_shot(shot, roles), shot, start, clip_max))
        candidates.sort(key=lambda c: (-c[0], c[2]))
        filled, clips = 0.0, []
        for score, shot, start, clip_max in candidates:
            if filled >= budget - 0.3:
                break
            dur = round(min(clip_max, shot.duration_s, budget - filled), 2)
            if dur < 0.4:
                continue
            # Punch dialogue: start at the shot head so the line is heard; otherwise take the middle.
            offset = 0.0 if section in ("punch", "stinger") else max(0.0, (shot.duration_s - dur) / 2)
            clips.append(TrailerClip(section, shot.shot_id, round(start + offset, 3), dur, score))
            used.add(shot.shot_id)
            filled += dur
        picked[section] = clips
    plan: list[TrailerClip] = []
    for section, share, _ in SECTIONS:
        if section == "title":
            plan.append(TrailerClip("title", None, 0.0, round(share * target_seconds, 2), 0.0))
        else:
            plan.extend(picked[section])
    return plan
