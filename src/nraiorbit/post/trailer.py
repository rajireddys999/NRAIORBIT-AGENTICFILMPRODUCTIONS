"""Trailer cutter: score shots, fill a Telugu trailer template, carry whole dialogue lines, avoid spoilers.

Template (2:15 by default): world -> hero -> conflict -> emotion -> escalation montage ->
punch dialogue -> title card -> stinger. Section shares scale to any target length.

Dialogue is never chopped out of the film's sound. A few whole lines (hero intro, villain threat, one emotion line,
the punch line last before the title card) each get a clip of their own: the picture under the line plus 0.2 s on
either side. The trailer's audio is those lines laid over the trailer score; every other clip keeps the fast cutting.
"""

from __future__ import annotations

import math
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
LINE_ORDER = ["punch", "hero", "conflict", "emotion", "stinger"]     # sections that may carry a line, picked in order
SPOILER_CUTOFF = 0.88        # nothing from the final 12% of the film
CLIMAX_REGION = 0.80         # shots after this point appear only as escalation-montage glimpses
CLIMAX_GLIMPSE_MAX_S = 0.8
LINE_PAD_S = 0.2             # picture before and after a carried line
LINE_SHIFT_MAX_S = 2.0       # a line's picture may slide back this far to stay on generated shots
DIALOGUE_MAX_SHARE = 0.6     # carried lines fill at most this share of the trailer
LINE_DONORS = {"escalation": 0.08, "world": 0.06}    # lend time to lines longer than their section, down to this share
HEROIC = ("veera", "raudra", "adbhuta")
PUNCH_DROP_S = 0.7           # near-silence in the score right before the punch line
ARC = {                      # score direction per section, for the music generator
    "world": "world: calm, sparse flute and veena over a low drone",
    "hero": "hero entry: thavil and dappu build, rising",
    "conflict": "conflict: menace, low brass, chenda hits and dark strings",
    "emotion": "emotion: pull back to a solo violin and flute, soft",
    "escalation": "escalation: full percussion, fast teenmaar groove rising to the peak",
    "punch": "punch: hold the tension on sustained strings and heartbeat sub hits",
    "stinger": "stinger tail: one last groove phrase, then cut to silence",
}


@dataclass
class TrailerLine:
    """A dialogue line as placed in the film (timeline.json), with what the cutter needs to choose it."""
    line_id: str
    film_start_s: float
    duration_s: float
    speaker: str | None = None      # character id
    rasa: str = "shanta"
    intensity: int = 5


@dataclass
class TrailerClip:
    section: str
    shot_id: str | None      # None = title card
    film_start_s: float
    duration_s: float
    score: float
    line_id: str | None = None      # whole dialogue line this clip carries
    line_at_s: float = 0.0          # where that line starts in the trailer
    line_duration_s: float = 0.0
    trailer_start_s: float = 0.0


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


def _line_fits(section: str, line: TrailerLine, roles: dict[str, str], punch_ids: set[str]) -> bool:
    who = roles.get(line.speaker or "")
    if section == "punch":
        return line.line_id in punch_ids or (who == "hero" and line.rasa in HEROIC)
    if section == "hero":
        return who == "hero"
    if section == "conflict":
        return who == "villain" or (who != "hero" and line.rasa in ("raudra", "bhayanaka"))
    if section == "emotion":
        return line.rasa in ("karuna", "shringara", "vatsalya")
    if section == "stinger":
        return line.rasa == "hasya" or line.duration_s <= 3.5
    return False


def score_line(section: str, line: TrailerLine, roles: dict[str, str], punch_ids: set[str], film_seconds: float,
               max_line_s: float) -> float:
    who = roles.get(line.speaker or "")
    short = max(0.0, 1 - line.duration_s / max_line_s)       # trailer lines land better short
    heat = line.intensity / 10
    return round({
        "punch": 0.35 * (line.line_id in punch_ids) + 0.25 * (who == "hero") + 0.2 * heat + 0.2 * short,
        "hero": 0.4 * (line.rasa in HEROIC) + 0.4 * (1 - line.film_start_s / film_seconds) + 0.2 * short,  # intro
        "conflict": 0.35 * (line.rasa in ("raudra", "bhayanaka")) + 0.35 * heat + 0.3 * short,
        "emotion": 0.4 * heat + 0.3 * (who in ("hero", "heroine")) + 0.3 * short,
        "stinger": 0.5 * (line.rasa == "hasya") + 0.5 * short,
    }[section], 4)


def _generated_spans(shots: list[tuple[Shot, float]]) -> list[list[float]]:
    """Stretches of the film covered by consecutive usable shots."""
    spans: list[list[float]] = []
    for shot, start in sorted(shots, key=lambda s: s[1]):
        if spans and start <= spans[-1][1] + 1e-3:
            spans[-1][1] = max(spans[-1][1], start + shot.duration_s)
        else:
            spans.append([start, start + shot.duration_s])
    return spans


def _line_window(line: TrailerLine, length: float, spans: list[list[float]]) -> float | None:
    """Film time where a carried line's picture starts: LINE_PAD_S before the line, slid back (at most
    LINE_SHIFT_MAX_S) so it stays on usable shots; None if that stretch is too short for the whole line.
    A slid clip still plays the line LINE_PAD_S in, whole; its picture then leads the line by the slide."""
    want = line.film_start_s - LINE_PAD_S
    for a, b in spans:
        if a - 1e-6 <= line.film_start_s < b:
            start = max(a, min(want, b - length))
            return start if start + length <= b + 1e-6 and want - start <= LINE_SHIFT_MAX_S + 1e-6 else None
    return None


def _spoken_on_screen(shot: Shot, lines: list[TrailerLine]) -> set[str]:
    """Lines whose speaker is in frame in this shot. The video model animates that speaker's mouth, so the shot may
    only appear in the trailer with that very line on the soundtrack (and lip-synced), never silent."""
    return {ln.line_id for ln in lines if ln.line_id in shot.block_ids and ln.speaker in shot.characters_in_frame}


def _carry_lines(shots: list[tuple[Shot, float]], lines: list[TrailerLine], roles: dict[str, str],
                 film_seconds: float, target_seconds: float, budgets: dict[str, float], max_line_s: float,
                 max_lines: int, fps: int) -> dict[str, TrailerClip]:
    """One whole line per dialogue section, best first. A line longer than its section borrows time from the
    escalation montage and the world opening (budgets is updated), so the trailer keeps its length."""
    spans = _generated_spans(shots)
    punch_ids = {b for shot, _ in shots if shot.punch_dialogue for b in shot.block_ids}
    spare = {s: budgets[s] - floor * target_seconds for s, floor in LINE_DONORS.items()}
    room = DIALOGUE_MAX_SHARE * target_seconds
    carried: dict[str, TrailerClip] = {}
    for section in LINE_ORDER:
        if len(carried) >= max_lines:
            break
        taken = {c.line_id for c in carried.values()}
        ranked = sorted(((score_line(section, ln, roles, punch_ids, film_seconds, max_line_s), ln) for ln in lines
                         if ln.line_id not in taken and ln.duration_s <= max_line_s
                         and _line_fits(section, ln, roles, punch_ids)), key=lambda c: (-c[0], c[1].film_start_s))
        for score, ln in ranked:
            length = math.ceil((ln.duration_s + 2 * LINE_PAD_S) * fps - 1e-6) / fps
            start = _line_window(ln, length, spans)
            if start is None or start > CLIMAX_REGION * film_seconds or start + length > SPOILER_CUTOFF * film_seconds:
                continue
            if any(start < c.film_start_s + c.duration_s and c.film_start_s < start + length for c in carried.values()):
                continue                                         # same picture twice
            if any(t < start + length and start < t + s.duration_s and _spoken_on_screen(s, lines) - {ln.line_id}
                   for s, t in shots):
                continue                                         # another speaker would mouth words with no voice
            extra = max(0.0, length - budgets[section])
            if extra > sum(spare.values()) + 1e-9 or length > room + 1e-9:
                continue
            for donor in spare:
                take = min(extra, spare[donor])
                spare[donor] -= take
                budgets[donor] -= take
                extra -= take
            budgets[section] = max(budgets[section], length)
            room -= length
            # the shot the clip starts in; never None, which the cutter reads as the title card
            shot_id = max(((t, s.shot_id) for s, t in shots if t <= start + 1e-3), default=(0.0, shots[0][0].shot_id))[1]
            carried[section] = TrailerClip(section, shot_id, round(start, 3), round(length, 4), score, ln.line_id,
                                           line_duration_s=round(ln.duration_s, 3))
            break
    return carried


def plan_trailer(shots: list[tuple[Shot, float]], bible: StoryBible, film_seconds: float, target_seconds: float,
                 lines: list[TrailerLine] = (), *, max_line_s: float = 7.5, max_lines: int = 5,
                 fps: int = 24) -> list[TrailerClip]:
    """shots: (shot, film_start_s) for every usable shot of the film, in order. lines: the film's placed dialogue.
    Clip lengths are whole frames, so trailer times (trailer_start_s, line_at_s) match the cut exactly."""
    roles = {c.id: c.role for c in bible.characters}
    shares = {name: (share, max_clip) for name, share, max_clip in SECTIONS}
    budgets = {name: share * target_seconds for name, share, _ in SECTIONS}
    carried = _carry_lines(shots, list(lines), roles, film_seconds, target_seconds, budgets, max_line_s, max_lines, fps)
    used = {shot.shot_id for shot, start in shots       # picture under a carried line is not cut in again
            if any(start < c.film_start_s + c.duration_s and c.film_start_s < start + shot.duration_s
                   for c in carried.values())}
    picked: dict[str, list[TrailerClip]] = {}
    for section in SELECTION_ORDER:
        share, max_clip = shares[section]
        budget = budgets[section] - (carried[section].duration_s if section in carried else 0.0)
        candidates = []
        for shot, start in shots:
            pos = start / film_seconds
            if shot.shot_id in used or pos > SPOILER_CUTOFF or not _section_fits(section, shot, roles):
                continue
            if _spoken_on_screen(shot, list(lines)):          # a talking face needs its line: only carried clips
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
            dur = math.floor(min(clip_max, shot.duration_s, budget - filled) * fps + 1e-6) / fps
            if dur < 0.4:
                continue
            offset = max(0.0, (shot.duration_s - dur) / 2)       # the middle of the shot; sound comes from lines
            clips.append(TrailerClip(section, shot.shot_id, round(start + offset, 3), round(dur, 4), score))
            used.add(shot.shot_id)
            filled += dur
        picked[section] = clips
    plan: list[TrailerClip] = []
    for section, share, _ in SECTIONS:
        if section == "title":
            plan.append(TrailerClip("title", None, 0.0, round(max(1, round(share * target_seconds * fps)) / fps, 4), 0.0))
            continue
        plan.extend(picked[section])
        if section in carried:                                   # the line closes its section: punch, then title
            plan.append(carried[section])
    t = 0.0
    for clip in plan:
        clip.trailer_start_s = round(t, 4)
        if clip.line_id:
            clip.line_at_s = round(t + LINE_PAD_S, 4)
        t += clip.duration_s
    return plan


def elevation_arc(plan: list[TrailerClip]) -> tuple[str, float]:
    """Timed direction for the trailer score, from the actual cut (exact second marks), and the time the
    near-silence drop before the punch line starts."""
    spans: dict[str, list[float]] = {}
    for c in plan:
        span = spans.setdefault(c.section, [c.trailer_start_s, c.trailer_start_s])
        span[1] = max(span[1], c.trailer_start_s + c.duration_s)
    title = spans["title"][0] if "title" in spans else sum(c.duration_s for c in plan)
    punch = next((c for c in plan if c.section == "punch" and c.line_id), None)
    drop = round(max(0.0, (punch.line_at_s if punch else title) - PUNCH_DROP_S), 2)
    marks = [(a, f"{a:.1f}-{b:.1f}s {ARC[s]}") for s, (a, b) in spans.items() if s in ARC and b > a]
    marks += [(drop, f"{drop:.1f}s: {PUNCH_DROP_S:.1f} s near-silence drop before the "
                     f"{'punch line' if punch else 'title hit'}"),
              (title, f"{title:.1f}s: huge hit on the title card, full percussion, brass and choir")]
    said = [f"{c.line_at_s:.1f}-{c.line_at_s + c.line_duration_s:.1f}s" for c in plan if c.line_id]
    notes = ("Telugu mass trailer score timed to the cut: " + "; ".join(m for _, m in sorted(marks, key=lambda m: m[0]))
             + "." + (f" Dialogue at {', '.join(said)}: keep the bed low and sparse under it." if said else ""))
    return notes, drop
