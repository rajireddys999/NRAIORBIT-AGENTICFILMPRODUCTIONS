"""Runtime budget: structure templates, outline validation and duration fitting.

LLMs are bad at arithmetic over 80 scenes, so the Screenwriter proposes durations and
this module enforces them exactly (rebalance) and checks structural landmarks such as
the hero intro and the interval bang.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schemas import Outline, ShotList

# (block name, start fraction, end fraction, set pieces expected inside the block)
TEMPLATES: dict[str, list[tuple[str, float, float, list[str]]]] = {
    "telugu_commercial": [
        ("opening_world", 0.00, 0.076, []),
        ("hero_intro", 0.076, 0.143, ["hero_intro"]),
        ("act1_development", 0.143, 0.333, []),
        ("act2a_escalation", 0.333, 0.524, []),
        ("interval_bang", 0.524, 0.552, ["interval_bang"]),
        ("act2b_flashback", 0.552, 0.762, []),
        ("pre_climax", 0.762, 0.857, []),
        ("climax", 0.857, 0.952, ["climax"]),
        ("resolution", 0.952, 1.00, []),
    ],
    "family_drama": [
        ("family_world", 0.00, 0.15, []),
        ("disruption", 0.15, 0.35, []),
        ("interval_bang", 0.50, 0.55, ["interval_bang"]),
        ("emotional_core", 0.55, 0.80, ["emotional_peak"]),
        ("climax", 0.85, 0.95, ["climax"]),
        ("resolution", 0.95, 1.00, []),
    ],
    "thriller": [
        ("hook", 0.00, 0.08, []),
        ("investigation", 0.08, 0.50, []),
        ("interval_bang", 0.50, 0.55, ["interval_bang"]),
        ("twists", 0.55, 0.85, []),
        ("climax", 0.85, 0.96, ["climax"]),
        ("resolution", 0.96, 1.00, []),
    ],
    "village_action": [
        ("village_world", 0.00, 0.08, []),
        ("hero_intro", 0.08, 0.15, ["hero_intro"]),
        ("feud_setup", 0.15, 0.50, []),
        ("interval_bang", 0.50, 0.56, ["interval_bang"]),
        ("feud_war", 0.56, 0.85, []),
        ("climax", 0.85, 0.96, ["climax"]),
        ("resolution", 0.96, 1.00, []),
    ],
}

# Where each landmark may start, as a fraction of runtime (lenient windows).
LANDMARK_WINDOWS = {
    "hero_intro": (0.0, 0.20),
    "interval_bang": (0.45, 0.62),
    "climax": (0.75, 0.98),
}


@dataclass
class Block:
    name: str
    start_s: float
    end_s: float
    set_pieces: list[str]


def template_blocks(template: str, runtime_minutes: float) -> list[Block]:
    total = runtime_minutes * 60
    return [Block(n, round(a * total, 1), round(b * total, 1), sp) for n, a, b, sp in TEMPLATES[template]]


def describe_template(template: str, runtime_minutes: float) -> str:
    lines = [f"Structure template '{template}' for {runtime_minutes:g} minutes:"]
    for b in template_blocks(template, runtime_minutes):
        sp = f" (must contain: {', '.join(b.set_pieces)})" if b.set_pieces else ""
        lines.append(f"- {b.name}: {b.start_s / 60:.1f}-{b.end_s / 60:.1f} min{sp}")
    return "\n".join(lines)


def scene_start_times(outline: Outline) -> dict[str, float]:
    t, starts = 0.0, {}
    for sc in outline.scenes:
        starts[sc.scene_id] = t
        t += sc.target_seconds
    return starts


def rebalance_outline(outline: Outline, runtime_minutes: float, min_scene_s: int = 20, step: int = 1) -> Outline:
    """Scale scene durations so they sum to exactly runtime_minutes. With step 2 every scene lands on an even number
    of seconds (what shots of 4, 6 and 8 s can add up to); the drift goes to the longest scene."""
    target = int(round(runtime_minutes * 60))
    current = sum(s.target_seconds for s in outline.scenes)
    if current <= 0:
        raise ValueError("outline has no duration")
    factor = target / current
    step = max(1, int(step))
    scenes = [s.model_copy(update={"target_seconds": max(min_scene_s, int(round(s.target_seconds * factor / step)) * step)})
              for s in outline.scenes]
    drift = target - sum(s.target_seconds for s in scenes)
    longest = max(range(len(scenes)), key=lambda i: scenes[i].target_seconds)
    scenes[longest] = scenes[longest].model_copy(update={"target_seconds": scenes[longest].target_seconds + drift})
    return Outline(scenes=scenes)


def validate_outline(outline: Outline, runtime_minutes: float, tolerance_minutes: float) -> list[str]:
    issues: list[str] = []
    total = sum(s.target_seconds for s in outline.scenes)
    if abs(total - runtime_minutes * 60) > tolerance_minutes * 60:
        issues.append(f"total runtime {total / 60:.1f} min, target {runtime_minutes} ± {tolerance_minutes}")
    ids = [s.scene_id for s in outline.scenes]
    if len(ids) != len(set(ids)):
        issues.append("duplicate scene ids")
    starts = scene_start_times(outline)
    for landmark, (lo, hi) in LANDMARK_WINDOWS.items():
        scenes = [s for s in outline.scenes if s.set_piece == landmark]
        if not scenes:
            if landmark in ("interval_bang", "climax"):
                issues.append(f"no scene marked '{landmark}'")
            continue
        frac = starts[scenes[0].scene_id] / max(total, 1)
        if not lo <= frac <= hi:
            issues.append(f"'{landmark}' starts at {frac:.0%} of runtime, expected {lo:.0%}-{hi:.0%}")
    return issues


def fit_shots_to_grid(shots: ShotList, target_seconds: float, allowed) -> ShotList | None:
    """Shot durations on the lengths the video model bills (Veo: 4, 6 or 8 s; 8 s only at 1080p), adding up to the
    scene target exactly, so no generated second is paid for and then trimmed away. `allowed` is the list of lengths,
    or a function of the shot giving its list. Shots keep their proportions as far as the grid allows (a shot the
    director wanted longest stays longest or equal). None when the target cannot be met on the grid: an odd target,
    too few or too many shots for it; the caller then fits proportionally as before."""
    if not shots.shots:
        return shots
    options = [sorted({int(a) for a in (allowed(s) if callable(allowed) else allowed)}) for s in shots.shots]
    if any(not o for o in options):
        return None
    target = int(round(target_seconds))
    if abs(target - target_seconds) > 1e-6:
        return None
    if not sum(o[0] for o in options) <= target <= sum(o[-1] for o in options):
        return None
    current = sum(s.duration_s for s in shots.shots)
    factor = target / current if current > 0 else 1.0
    want = [s.duration_s * factor for s in shots.shots]
    pick = [min(o, key=lambda a, w=w: (abs(a - w), a)) for o, w in zip(options, want)]
    for _ in range(4 * len(pick) + 4):                     # each pass moves one shot one size; bounded, never loops
        remainder = target - sum(pick)
        if remainder == 0:
            return ShotList(scene_id=shots.scene_id,
                            shots=[s.model_copy(update={"duration_s": float(d)}) for s, d in zip(shots.shots, pick)])
        if remainder > 0:
            room = [i for i, (o, d) in enumerate(zip(options, pick)) if d < o[-1]]
            if not room:
                return None
            i = max(room, key=lambda i: want[i] - pick[i])        # the shot squeezed the most grows first
            pick[i] = next(a for a in options[i] if a > pick[i])
        else:
            room = [i for i, (o, d) in enumerate(zip(options, pick)) if d > o[0]]
            if not room:
                return None
            i = min(room, key=lambda i: want[i] - pick[i])        # the shot stretched the most shrinks first
            pick[i] = next(a for a in reversed(options[i]) if a < pick[i])
    return None


def fit_shots_to_scene(shots: ShotList, target_seconds: float, fps: int = 24,
                       min_shot: float = 1.0, max_shot: float = 12.0) -> ShotList:
    """Scale shot durations to the scene budget, in whole frames, clamped to generator-friendly lengths.

    Working in frames keeps the edit free of cumulative drift across a 1,500-shot film.
    """
    if not shots.shots:
        return shots
    current = sum(s.duration_s for s in shots.shots)
    target_frames = int(round(target_seconds * fps))
    lo, hi = int(round(min_shot * fps)), int(round(max_shot * fps))
    factor = target_seconds / current if current > 0 else 1.0
    frames = [min(hi, max(lo, int(round(s.duration_s * factor * fps)))) for s in shots.shots]
    # Spread the remainder over the shots that still have room, longest first.
    remainder = target_frames - sum(frames)
    order = sorted(range(len(frames)), key=lambda i: -frames[i])
    relaxed = False
    while remainder != 0:
        movable = [i for i in order if (remainder > 0 and frames[i] < hi) or (remainder < 0 and frames[i] > lo)]
        if not movable:
            if relaxed:
                break
            # Clamps make the target unreachable: scene timing wins. Shots past max_shot are
            # rendered as a base clip plus extensions by the video stage.
            lo, hi, relaxed = 1, max(target_frames, 1), True
            continue
        for i in movable:
            if remainder == 0:
                break
            step = 1 if remainder > 0 else -1
            frames[i] += step
            remainder -= step
    fitted = [s.model_copy(update={"duration_s": f / fps}) for s, f in zip(shots.shots, frames)]
    return ShotList(scene_id=shots.scene_id, shots=fitted)
