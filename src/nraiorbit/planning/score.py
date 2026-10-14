"""Score to picture: music style presets and timed sections for generated cues.

Presets live in config/music_styles.yaml; a project picks one per slot under music.styles, e.g.

    music:
      styles: {elevation: folk_mass, emotion: violin_swell}

A cue in a slot with a chosen preset gets Cue.notes written as timed sections ("[0:00 - 0:09] Build: ..."),
placed on the film: an elevation cue hits on its strongest shot (the hero reveal), an emotional cue peaks on
its most intense shot. The music provider adds the preset's overall style line. Cues that already carry notes
(the trailer's arc) keep them.
"""

from __future__ import annotations

from ..config import DEFAULT_CONFIG_DIR, load_yaml
from ..schemas import Cue, Shot

STYLE_FILE = DEFAULT_CONFIG_DIR / "music_styles.yaml"
DROP_S = 0.8          # near-silence right before an elevation hit
HIT_S = 2.5
OUTRO_S = 3.0
BREATH_S = 1.2        # near-silence before an emotional peak


def slot_for(cue: Cue) -> str:
    if cue.cue_type == "trailer":
        return "trailer"
    if cue.rasa == "veera":
        return "elevation" if cue.peak or cue.intensity >= 8 else "action"
    if cue.rasa == "raudra":
        return "villain"
    if cue.rasa in ("karuna", "vatsalya"):
        return "emotion"
    if cue.rasa == "shringara":
        return "romance"
    if cue.rasa in ("bhayanaka", "bibhatsa"):
        return "tension"
    return "calm"


def load_styles(cfg: dict) -> dict[str, dict]:
    """slot -> chosen preset (with its name), from music.styles; the trailer falls back to the elevation pick."""
    music = cfg.get("music") or {}
    chosen = dict(music.get("styles") or {})
    if not chosen:
        return {}
    presets = {**(load_yaml(STYLE_FILE).get("presets") or {}), **(music.get("presets") or {})}
    chosen.setdefault("trailer", chosen.get("elevation"))
    out = {}
    for slot, name in chosen.items():
        if not name:
            continue
        if name not in presets:
            raise KeyError(f"music.styles.{slot}: unknown preset '{name}' (see {STYLE_FILE.name})")
        out[slot] = {"name": name, **presets[name]}
    return out


def peak_offset(cue: Cue, spans: list[tuple[float, Shot]]) -> float | None:
    """Seconds into the audible cue where its strongest shot starts. spans: (film start, shot) for every shot."""
    start = cue.start_s + cue.silence_before_s
    end = cue.start_s + cue.duration_s
    inside = [(t, s) for t, s in spans if start <= t < end]
    if not inside:
        return None
    t, _ = max(inside, key=lambda ts: (ts[1].intensity, ts[1].punch_dialogue, ts[1].trailer_candidate, -ts[0]))
    return t - start


def _clock(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def _sections(rows: list[tuple[float, float, str, str]]) -> str:
    return " ".join(f"[{_clock(a)} - {_clock(b)}] {name}: {' '.join(text.split()).rstrip('.')}."
                    for a, b, name, text in rows if b - a >= 0.75 and text)


def timed_structure(cue: Cue, preset: dict, peak_at: float | None) -> str:
    length = cue.duration_s - cue.silence_before_s
    sec = preset.get("sections") or {}
    if preset.get("shape", "elevation") == "emotion":
        p = min(max(peak_at if peak_at is not None else 0.7 * length, 0.45 * length), length - 5)
        rise = 0.45 * p
        top = min(length - OUTRO_S, p + 0.22 * length)
        return _sections([(0, rise, "Opening", sec.get("opening", "")),
                          (rise, p - BREATH_S, "Build", sec.get("build", "")),
                          (p - BREATH_S, p, "Breath", sec.get("breath", "")),   # silence before the swell: chills
                          (p, top, "Peak", sec.get("peak", "")), (top, length, "Release", sec.get("release", ""))])
    p = min(max(peak_at if peak_at is not None else 0.4 * length, min(6.0, 0.3 * length)), length - 6)
    intro = 0.4 * p if p >= 8 else 0.0
    return _sections([(0, intro, "Intro", sec.get("intro", "")), (intro, p - DROP_S, "Build", sec.get("build", "")),
                      (p - DROP_S, p, "Drop", sec.get("drop", "")), (p, p + HIT_S, "Hit", sec.get("hit", "")),
                      (p + HIT_S, length - OUTRO_S, "Drive", sec.get("drive", "")),
                      (length - OUTRO_S, length, "Outro", sec.get("outro", ""))])


def film_spans(root) -> list[tuple[float, Shot]]:
    """(film start, shot) for every planned shot, timed exactly as Pipeline._render places them."""
    from ..planning.runtime import scene_start_times
    from ..schemas import Outline, ShotList

    outline = Outline.model_validate_json((root / "02_outline.json").read_text(encoding="utf-8"))
    spans, starts = [], scene_start_times(outline)
    for sc in outline.scenes:
        path, t = root / "04_shots" / f"{sc.scene_id}.json", starts[sc.scene_id]
        for shot in (ShotList.model_validate_json(path.read_text(encoding="utf-8")).shots if path.exists() else []):
            spans.append((round(t, 3), shot))
            t += shot.duration_s
    return spans


def score_cue(cue: Cue, spans: list[tuple[float, Shot]], styles: dict[str, dict]) -> Cue:
    """The cue with timed sections from its slot's preset; unchanged without a preset or when it has notes."""
    preset = styles.get(slot_for(cue))
    if not preset or cue.notes.strip() or not preset.get("sections"):
        return cue
    return cue.model_copy(update={"notes": timed_structure(cue, preset, peak_offset(cue, spans))})
