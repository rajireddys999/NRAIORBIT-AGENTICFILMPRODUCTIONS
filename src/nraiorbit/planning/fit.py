"""Whether the dialogue fits the picture. A line is heard over the shots that carry it (the shots whose block_ids name
it): it starts DIALOGUE_LEAD_IN_S after the first of them begins and must end by the end of the last of them (a
breath of OVERRUN_TOL_S into the next shot is fine). Lines never overlap, so a line waits for the one before it; when
a scene holds more speech than its shots, lines drift off their pictures, voices play over the wrong faces and the
last lines fall past the end of the film. Everything here is checked before money is spent:

    scene lengths   a scene is made long enough for its lines (SCENE_SPEECH_SHARE), up to film.max_runtime_minutes;
                    past that the screenwriter shortens them (Pipeline._fit_scene_lengths)
    shot plans      every line in a shot long enough for it, with its speaker in frame (Pipeline._shots)
    voiced lines    measured lines that run past their shots stop the run before lip-sync (Pipeline._render), and
                    the screenwriter shortens them (service/produce.fit_dialogue)
    the cut         every line checked against the picture before the owner is shown it (sync_problems)

Pure functions over plain data, so planning, the render step and the checks share one rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

DIALOGUE_LEAD_IN_S = 0.2      # a line starts this long after its first shot begins
DIALOGUE_GAP_S = 0.25         # breath between consecutive lines
OVERRUN_TOL_S = 0.5           # a line may end this far into the shot after its own
SPEECH_BASE_S = 0.3           # measured on ElevenLabs Telugu: about 0.09 s a character (median), 0.115 s the slowest
SPEECH_S_PER_CHAR = 0.095     # tenth; the estimate errs a little long
SCENE_SPEECH_SHARE = 0.75     # a scene's lines (with lead-ins and breaths) fill at most this share of it
MAX_RUNTIME_FACTOR = 5 / 3    # without film.max_runtime_minutes: a 3-minute film may grow to 5 to fit its dialogue
TOO_WIDE = ("EWS", "WS", "aerial")   # a speaker this far away is too small for lip-sync to find


def text_sha(text: str) -> str:
    """A short fingerprint of a line's words: a measured length holds only while the words are the ones voiced."""
    import hashlib
    return hashlib.sha256((text or "").encode()).hexdigest()[:12]


def speech_seconds(text_te: str) -> float:
    """How long a Telugu line takes to say, estimated from its length (letters, not spaces)."""
    chars = len("".join((text_te or "").split()))
    return round(SPEECH_BASE_S + SPEECH_S_PER_CHAR * chars, 2) if chars else 0.0


def chars_for(seconds: float) -> int:
    """The most Telugu letters a line of `seconds` may have, by the same estimate."""
    return max(4, int((seconds - SPEECH_BASE_S) / SPEECH_S_PER_CHAR))


def scene_speech(blocks) -> float:
    """The seconds a scene's dialogue takes with its lead-ins and breaths, estimated."""
    lines = [b for b in blocks if b.kind == "dialogue"]
    return round(sum(speech_seconds(b.text_te) for b in lines) + len(lines) * (DIALOGUE_LEAD_IN_S + DIALOGUE_GAP_S), 2)


def max_runtime_s(film: dict) -> float:
    """The longest the film may run to fit its dialogue: film.max_runtime_minutes, else MAX_RUNTIME_FACTOR times
    film.runtime_minutes."""
    if film.get("max_runtime_minutes"):
        return float(film["max_runtime_minutes"]) * 60
    return float(film.get("runtime_minutes") or 0) * 60 * MAX_RUNTIME_FACTOR


def lengthen_scenes(lengths: dict[str, float], speech: dict[str, float], cap_s: float, step: int = 2) -> \
        tuple[dict[str, float], dict[str, float]]:
    """Scene lengths that hold their speech: each scene at least its speech / SCENE_SPEECH_SHARE, rounded up to
    `step` seconds, never shorter than planned. Over cap_s, every scene that had to grow grows by the same share of
    what it needed (the film ends at the cap) and gives back the rest as speech to cut. Returns (new lengths,
    {scene: seconds of speech to cut}); the second is empty when the film fits."""
    need = {sid: float(max(planned, math.ceil(speech.get(sid, 0.0) / SCENE_SPEECH_SHARE / step) * step))
            for sid, planned in lengths.items()}
    growth = sum(need[s] - lengths[s] for s in lengths)
    room = cap_s - sum(lengths.values())
    if sum(need.values()) <= cap_s or growth <= 0:
        return need, {}
    share = max(0.0, room) / growth
    new, cut = {}, {}
    for sid, planned in lengths.items():
        grown = planned + math.floor((need[sid] - planned) * share / step) * step
        new[sid] = float(grown)
        if need[sid] > grown:
            cut[sid] = round((need[sid] - grown) * SCENE_SPEECH_SHARE, 1)
    return new, cut


@dataclass
class Line:
    """A dialogue line placed (or to be placed) in the film."""
    block_id: str
    speaker: str | None
    scene_id: str
    earliest_s: float             # its first shot's start + DIALOGUE_LEAD_IN_S
    window_end_s: float           # the end of the last shot that carries it
    dur_s: float
    shots: list[str] = field(default_factory=list)
    start_s: float = 0.0

    @property
    def end_s(self) -> float:
        return self.start_s + self.dur_s

    def overrun_s(self, film_end: float) -> float:
        """How far the line runs past where it may end (its shots, plus OVERRUN_TOL_S; never past the film)."""
        return round(max(self.end_s - (self.window_end_s + OVERRUN_TOL_S), self.end_s - film_end, 0.0), 2)

    @property
    def room_s(self) -> float:
        """The longest this line could be, starting where it may start and ending where it must end."""
        return round(self.window_end_s + OVERRUN_TOL_S - self.earliest_s, 2)


def place_start(earliest: float, dur: float, prev_end: float, film_end: float, head_start: float) -> float:
    """Where a line starts: at its earliest, or when the line before it has ended; only the film's last lines are
    pulled earlier (to head_start at most, their first shot's start) to end with the film. Lines never overlap."""
    start = max(earliest, prev_end + DIALOGUE_GAP_S)
    if start + dur > film_end:
        start = max(prev_end + DIALOGUE_GAP_S, min(start, max(head_start, film_end - dur)))
    return round(start, 3)


def plan_lines(scenes, durations: dict[str, float], film_end: float | None = None) -> list[Line]:
    """The film's lines as the render step places them. `scenes`: [(scene_id, scene_start_s, shots, blocks)] in film
    order, shots with shot_id/duration_s/block_ids, blocks with block_id/kind/character_id/text_te. A line's duration
    comes from `durations` (measured; 0 drops it) or is estimated (speech_seconds). film_end: where the film ends (its last
    lines are pulled earlier to end with it); by default the end of the last scene's shots."""
    lines, prev_end = [], -DIALOGUE_GAP_S
    if film_end is None:
        film_end = max((start + sum(s.duration_s for s in shots) for _, start, shots, _ in scenes), default=0.0)
    for scene_id, start, shots, blocks in scenes:
        by_id = {b.block_id: b for b in blocks}
        t, cover = start, {}
        for shot in shots:
            for bid in shot.block_ids:
                if bid in by_id and by_id[bid].kind == "dialogue":
                    first, _, ids = cover.get(bid, (t, 0.0, []))
                    cover[bid] = (first, t + shot.duration_s, ids + [shot.shot_id])
            t += shot.duration_s
        for bid, (first, last_end, ids) in sorted(cover.items(), key=lambda kv: kv[1][0]):
            b = by_id[bid]
            dur = durations[bid] if bid in durations else speech_seconds(b.text_te)
            if dur <= 0:
                continue                                   # a line given no length is dropped: it is not placed
            line = Line(bid, b.character_id, scene_id, round(first + DIALOGUE_LEAD_IN_S, 3), round(last_end, 3),
                        round(dur, 3), ids)
            line.start_s = place_start(line.earliest_s, dur, prev_end, film_end, first)
            prev_end = line.end_s
            lines.append(line)
    return lines


def uncovered(blocks, shots) -> list[str]:
    """Dialogue lines no shot carries (never heard)."""
    carried = {b for s in shots for b in s.block_ids}
    return [b.block_id for b in blocks if b.kind == "dialogue" and b.block_id not in carried]


def shot_problems(blocks, shots, scene_start: float, off_screen=(), voiced_off=()) -> list[str]:
    """What is wrong with a scene's shot plan for its dialogue: a line no shot carries, a line whose speaker is in
    none of the shots that carry it (except people kept off screen, and those whose lines are heard off screen by
    design), and lines that would not fit their shots (estimated lengths)."""
    problems = [f"line {bid} is in no shot" for bid in uncovered(blocks, shots)]
    by_id = {b.block_id: b for b in blocks}
    for b in blocks:
        if b.kind != "dialogue" or b.character_id in off_screen or b.character_id in voiced_off:
            continue
        carriers = [s for s in shots if b.block_id in s.block_ids]
        if carriers and not any(b.character_id in s.characters_in_frame for s in carriers):
            problems.append(f"line {b.block_id} ({b.character_id}) is heard over shots that do not show its speaker")
    for b in blocks:                                # lip-sync cannot find a face that small
        if b.kind != "dialogue" or b.character_id in off_screen or b.character_id in voiced_off:
            continue
        showing = [s for s in shots if b.block_id in s.block_ids and b.character_id in s.characters_in_frame]
        if showing and all(getattr(s, "size", None) in TOO_WIDE for s in showing):
            problems.append(f"line {b.block_id} is spoken in a wide shot only ({', '.join(s.shot_id for s in showing)}): "
                            "frame its speaker MCU or closer, or the mouth cannot be synced")
    for line in plan_lines([("scene", scene_start, shots, list(by_id.values()))], {}, film_end=float("inf")):
        if line.overrun_s(float("inf")) > 0:
            problems.append(f"line {line.block_id} needs about {line.dur_s:.1f} s but its shots leave "
                            f"{line.room_s:.1f} s: make its shots longer, or give it a shot of its own")
    return problems


def sync_problems(timeline: dict, scripts: dict, plans: dict, off_screen=(), lipsync: dict | None = None,
                  synced_by_provider: bool = True, no_lipsync=()) -> list[str]:
    """Every line in the cut checked against the picture: it is carried by shots that are in the film, it plays
    within them (OVERRUN_TOL_S), before the end of the film, and when its speaker is on screen that shot was not left
    unsynced. `timeline`: 10_edit/timeline.json (a shot the edit cuts into later is measured from its take's first
    frame, start_s - in_s); `scripts`: {block_id: block}; `plans`: {shot_id: shot}; `lipsync`: {shot_id: this
    render's record of reports/lipsync.jsonl}; `no_lipsync`: shots never lip-synced by design (turned off by the
    owner, or placeholders)."""
    film_end = float(timeline.get("film_seconds") or 0)
    at = {s["shot_id"]: (float(s["start_s"]) - float(s.get("in_s") or 0.0), float(s["start_s"]) + float(s["duration_s"]))
          for s in timeline["shots"]}
    problems = []
    for d in timeline["dialogue"]:
        bid, a = d.get("ref_id"), float(d["start_s"])
        z = a + float(d["duration_s"])
        block = scripts.get(bid)
        if block is None:
            continue
        carriers = [sid for sid, shot in plans.items() if bid in shot.block_ids and sid in at]
        if not carriers:
            problems.append(f"{bid}: no shot in the film carries it")
            continue
        first, last = min(at[c][0] for c in carriers), max(at[c][1] for c in carriers)
        if z > film_end + 1e-3:
            problems.append(f"{bid}: plays past the end of the film ({z:.1f} s of {film_end:.1f} s)")
        elif a < first - 1e-3 or z > last + OVERRUN_TOL_S + 1e-3:
            problems.append(f"{bid}: heard at {_mmss(a)}-{_mmss(z)}, its shots run {_mmss(first)}-{_mmss(last)}")
        if lipsync is not None and synced_by_provider and block.character_id not in off_screen:
            for sid in carriers:
                if sid in no_lipsync:
                    continue
                shot = plans[sid]
                rec = lipsync.get(sid) or {}
                if block.character_id in shot.characters_in_frame and rec.get("mode") == "not synced":
                    problems.append(f"{bid}: {block.character_id} is on screen in {sid} but not lip-synced "
                                    f"({rec.get('why') or 'no reason given'})")
    return problems


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"
