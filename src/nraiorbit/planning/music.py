"""Deterministic first-pass cue plan from the scene rasa curve (docs/MUSIC_TELUGU.md §1).

The Music Director agent then proposes revisions (raga, instruments, motifs, silences)
which `apply_revisions` merges, so the agent can never break the timing.
"""

from __future__ import annotations

from ..schemas import CastLock, Cue, CueSheet, Outline, StoryBible
from .runtime import scene_start_times

PEAK_SET_PIECES = {"hero_intro", "interval_bang", "climax"}


def _lerp_range(rng: list[int], intensity: int) -> int:
    lo, hi = rng
    return int(round(lo + (hi - lo) * (max(1, min(10, intensity)) - 1) / 9))


def _leads_by_role(bible: StoryBible) -> dict[str, str]:
    leads = {}
    for c in bible.characters:
        if c.role in ("hero", "heroine", "villain") and c.role not in leads:
            leads[c.role] = c.id
    return leads


def plan_cues(outline: Outline, bible: StoryBible, navarasa: dict, lock: CastLock | None = None) -> CueSheet:
    rules = navarasa.get("cue_rules", {})
    rasas = navarasa["rasas"]
    max_s = rules.get("max_cue_seconds", 240)
    merge = rules.get("merge_adjacent_same_rasa", True)
    silence = rules.get("silence_before_peak_seconds", 1.5)
    motif_roles = set(rules.get("leitmotif_roles", ["hero", "heroine", "villain"]))
    role_of = {c.id: c.role for c in bible.characters}
    starts = scene_start_times(outline)

    # 1. group scenes into runs: same rasa, peaks always stand alone, cap at max_s
    groups: list[list] = []
    for sc in outline.scenes:
        is_peak = sc.set_piece in PEAK_SET_PIECES
        prev = groups[-1] if groups else None
        can_merge = (
            merge and prev and not is_peak
            and prev[-1].rasa_primary == sc.rasa_primary
            and prev[-1].set_piece not in PEAK_SET_PIECES
            and sum(s.target_seconds for s in prev) + sc.target_seconds <= max_s
        )
        if can_merge:
            prev.append(sc)
        else:
            groups.append([sc])

    # 2. one cue per group
    cues: list[Cue] = []
    for i, group in enumerate(groups, 1):
        rasa = group[0].rasa_primary
        spec = rasas[rasa]["music"]
        intensity = max(s.intensity for s in group)
        peak = any(s.set_piece in PEAK_SET_PIECES for s in group)
        duration = sum(s.target_seconds for s in group)
        set_piece = next((s.set_piece for s in group if s.set_piece != "none"), "none")
        cue_types = spec.get("cue_types", ["bed"])
        cue_type = {"hero_intro": "hero_elevation", "interval_bang": "interval_bang", "climax": "climax"}.get(
            set_piece, cue_types[0])

        present = [cid for s in group for cid in s.characters]
        leitmotif = next((cid for cid in present if role_of.get(cid) in motif_roles), None)
        if lock and leitmotif:
            entry = lock.characters.get(leitmotif)
            if entry is None or entry.leitmotif_asset is None:
                leitmotif = None

        n_instruments = 3 + (intensity >= 5) + (intensity >= 8) * 2
        cues.append(Cue(
            cue_id=f"cue{i:03d}",
            scene_ids=[s.scene_id for s in group],
            start_s=float(starts[group[0].scene_id]),
            duration_s=float(duration),
            rasa=rasa,
            intensity=intensity,
            cue_type=cue_type,
            raga=spec["ragas"][0],
            tala=spec["talas"][0],
            bpm=_lerp_range(spec["tempo_bpm"], intensity),
            instruments=spec["instruments"][:n_instruments],
            leitmotif_character=leitmotif,
            peak=peak,
            silence_before_s=silence if peak else 0.0,
        ))
    return CueSheet(cues=cues)


def apply_revisions(sheet: CueSheet, revisions: list, navarasa: dict) -> tuple[CueSheet, list[str]]:
    """Merge agent revisions; reject ragas the score engine has no scale data for."""
    by_id = {c.cue_id: c for c in sheet.cues}
    known_ragas = set(navarasa.get("raga_scales", {})) | {"textural", "chromatic_hybrid"}
    notes: list[str] = []
    for rev in revisions:
        cue = by_id.get(rev.cue_id)
        if cue is None:
            notes.append(f"ignored revision for unknown cue {rev.cue_id}")
            continue
        update = {k: v for k, v in rev.model_dump().items()
                  if k not in ("cue_id", "reason") and v is not None}
        if "raga" in update and update["raga"] not in known_ragas:
            notes.append(f"{rev.cue_id}: raga '{update.pop('raga')}' has no scale data; kept {cue.raga}")
        by_id[rev.cue_id] = cue.model_copy(update=update)
        notes.append(f"{rev.cue_id}: {rev.reason}")
    return CueSheet(cues=[by_id[c.cue_id] for c in sheet.cues]), notes
