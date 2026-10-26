"""The film's sound effects, from the studio's library (custom_model_feed/sfx/, service/feed.py), laid into the
music bed so the dialogue ducks them as it ducks the score (post/assemble.master).

Two kinds, chosen from the words of the plan (no paid call):

    ambience   a quiet bed under a whole scene, looped to its length with soft edges: the scene's weather (rain,
               a flood, wind), else its place (forest, river, market, temple, village), else the night
    at a cut   a sound that belongs on the cut itself: a whoosh into a fast action shot, a deep boom under a slow-motion
               shot, thunder on lightning, an explosion, a vehicle passing

Hits on screen (a punch, a kick) are not placed here: they land on a frame inside the shot, which the plan does not
say, and a hit off its frame sounds worse than none. Sounds not in the library are simply not used: the film plays as
it would have without them. Each sound a film uses is copied into its own cache (cache/sfx/, backed up), so every
render plays the same sound whatever the library does later; the plan is kept in reports/sound_plan.json.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

# the scene's bed: (name, words anywhere in its heading/place/synopsis, words only in its heading or place, weather:
# heard indoors too, times of day it fits or None); the first that matches wins, so weather comes first
AMBIENCE = [
    ("flood_water", ("flood", "flooding", "floodwaters"), (), True, None),
    ("rain_heavy", ("rain", "raining", "rainy", "monsoon", "downpour"), (), True, None),
    ("wind", ("gale", "windy", "windstorm"), ("storm",), True, None),
    ("fire_crackling", ("burning", "flames", "blaze", "bonfire", "inferno"), (), True, None),
    ("river_flow", ("river", "stream", "waterfall", "riverbank"), ("ghat", "godavari", "krishna"), False, None),
    ("market_bustle", ("market", "bazaar", "marketplace"), (), False, None),
    ("temple_bells", ("temple",), (), False, None),
    ("forest_birds", ("forest", "jungle", "woods", "grove"), ("nallamala",), False, None),
    ("crowd_murmur", ("crowd", "festival", "jatara", "gathering"), (), False, None),
    ("village_morning", ("village", "farm", "fields", "paddy"), (), False, ("dawn", "morning")),
]
NIGHT = "night_crickets"                 # an outdoor night with nothing else to say
# the sound on the cut into a shot, from what the shot's motion says (never its picture: a parked jeep is silent)
AT_CUT = [
    ("slowmo_boom", ("slow motion", "slow-motion", "slowmo")),
    ("explosion", ("explodes", "explosion")),
    ("thunder", ("thunder", "thunderclap", "lightning strikes", "lightning flashes", "flash of lightning")),
    ("jeep_pass", ("jeep",)),
    ("bike_rev", ("motorcycle", "motorbike")),
    ("train_pass", ("train passes", "train rushes", "passing train", "train roars")),
    ("whoosh_fast", ("leaps", "lunges", "charges", "whip pan", "whip-pan", "spins", "dives")),
]
# the director's own sound cues (Shot.sfx_cues) that name one of the sounds above; a hit it names is not placed
CUE_NAMES = {"whoosh": "whoosh_fast", "whoosh_fast": "whoosh_fast", "boom": "slowmo_boom", "slowmo_boom": "slowmo_boom",
             "slow_mo_boom": "slowmo_boom", "explosion": "explosion", "thunder": "thunder", "jeep": "jeep_pass",
             "jeep_pass": "jeep_pass", "bike_rev": "bike_rev", "motorcycle": "bike_rev", "train": "train_pass",
             "train_pass": "train_pass"}
REF_DBFS = -20.0        # every sound is first brought to this level, then set under the music
AMBIENCE_DB = -18.0     # a bed: felt more than heard
AT_CUT_DB = -8.0
LEAD_S = {"whoosh_fast": 0.25}  # a whoosh starts a little before its cut, so it lands on it
GAP_S = 1.5             # sounds on cuts never closer than this
SAME_GAP_S = 6.0        # nor the same sound again within this
EDGE_S = 1.5            # a bed fades in and out over this
APPROVED_CUT = ("final_cut", "pacing")   # a cut approved before the film had a sound plan is never changed


def _says(text: str, words) -> bool:
    text = f" {text.lower()} "
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z-])", text) for w in words)


def scene_bed(scene) -> str | None:
    """The ambience a scene sits on (a name in the library's words), or None. Indoors only weather is heard."""
    heading = str(getattr(scene, "heading", "") or "")
    place = f"{heading} {getattr(scene, 'location_hint', '') or ''}"
    text = f"{place} {getattr(scene, 'synopsis', '') or ''}"
    indoors = heading.strip().upper().startswith("INT")
    when = str(getattr(scene, "time_of_day", "") or "")
    for name, anywhere, in_place, weather, times in AMBIENCE:
        if (indoors and not weather) or (times and when not in times):
            continue
        if _says(text, anywhere) or _says(place, in_place):
            return name
    return NIGHT if when == "night" and not indoors else None


def at_cut(shot) -> str | None:
    """The sound that belongs on the cut into this shot: the director's own cue when it names one, else what the
    shot's motion says; None for anything else (a hit lands inside the shot, on a frame the plan does not give)."""
    for cue in getattr(shot, "sfx_cues", None) or []:
        name = CUE_NAMES.get(str(cue).lower().strip())
        if name:
            return name
    text = str(getattr(shot, "motion_prompt", "") or "")
    for name, words in AT_CUT:
        if _says(text, words):
            return name
    return None


def plan_sound(scenes: list[tuple], shots: list[dict], plans: dict, have: set[str]) -> list[dict]:
    """The film's sound effects: [{name, kind (bed|cut), start_s, duration_s, gain_db, scene_id, shot_id}].
    `scenes`: [(scene outline, start_s, length_s)]; `shots`: the timeline's shots (shot_id, scene_id, start_s,
    duration_s, real); `plans`: shot_id -> Shot; `have`: the sound names the library holds. Sounds on cuts are
    spaced (GAP_S, SAME_GAP_S), so a run of slow motion or of leaps is not one boom after another."""
    out = []
    for scene, start, length in scenes:
        bed = scene_bed(scene)
        if bed in have and length > 2 * EDGE_S:
            out.append({"name": bed, "kind": "bed", "start_s": round(start, 3), "duration_s": round(length, 3),
                        "gain_db": AMBIENCE_DB, "scene_id": scene.scene_id, "shot_id": None})
    placed: list[dict] = []
    for s in sorted(shots, key=lambda x: float(x["start_s"])):
        plan = plans.get(s["shot_id"])
        name = at_cut(plan) if plan is not None and s.get("real", True) else None
        if name not in have:
            continue
        start = max(0.0, float(s["start_s"]) - LEAD_S.get(name, 0.0))
        if placed and (start - placed[-1]["start_s"] < GAP_S or any(
                p["name"] == name and start - p["start_s"] < SAME_GAP_S for p in placed)):
            continue
        placed.append({"name": name, "kind": "cut", "start_s": round(start, 3), "duration_s": None,
                       "gain_db": AT_CUT_DB, "scene_id": s.get("scene_id"), "shot_id": s["shot_id"]})
    return out + placed


def cut_mark(shots: list[dict]) -> str:
    """What identifies a cut: its shots and where they sit (a re-cut is a new one; a re-master of the same is not)."""
    import hashlib
    key = [(s["shot_id"], round(float(s["start_s"]), 2), round(float(s["duration_s"]), 2)) for s in shots]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


def library_sounds(feed_entries: list[dict]) -> dict[str, Path]:
    """name -> the newest library file of that sound."""
    best: dict[str, dict] = {}
    for e in feed_entries:
        name = e.get("mood")
        if name and (name not in best or (e.get("created") or "") > (best[name].get("created") or "")):
            best[name] = e
    return {name: Path(e["path"]) for name, e in best.items()}


def film_copies(root: Path, wanted: dict[str, Path]) -> dict[str, Path]:
    """The sounds a film uses, copied into its own cache/sfx/ the first time: name -> its copy (the film's copy
    wins, so a sound the library later replaces or loses never changes the film)."""
    folder = Path(root) / "cache" / "sfx"
    out = {}
    for name, src in wanted.items():
        mine = next(iter(sorted(folder.glob(f"{name}.*"))), None) if folder.exists() else None
        if mine is None and src.exists():
            folder.mkdir(parents=True, exist_ok=True)
            mine = folder / f"{name}{src.suffix}"
            shutil.copyfile(src, mine)
        if mine is not None:
            out[name] = mine
    return out


def looped(src: Path, seconds: float, out: Path) -> Path:
    """`src` looped to `seconds`, fading in and out over EDGE_S (a scene's bed), kept compressed (a long film's
    beds would not fit the runner's memory as WAV)."""
    from . import ffmpeg
    out.parent.mkdir(parents=True, exist_ok=True)
    fade = max(0.0, seconds - EDGE_S)
    ffmpeg.run(["-stream_loop", "-1", "-i", str(src), "-t", f"{seconds:.3f}",
                "-af", f"afade=t=in:d={EDGE_S},afade=t=out:st={fade:.3f}:d={EDGE_S}",
                "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(out)])
    return out


def _level(src: Path) -> float:
    """dB that brings a sound to REF_DBFS (0 when it cannot be measured)."""
    try:
        from .speech import level_gain_db
        return level_gain_db(src, REF_DBFS)
    except Exception:
        return 0.0


def film_sound(root: Path, scenes: list[tuple], shots: list[dict], plans: dict, feed_entries: list[dict],
               work: Path, log=print, extra_db: float = 0.0) -> list[tuple[Path, float, float]]:
    """The film's sound effects as (file, start_s, gain_db) for the music bed. The plan is decided once per cut
    (reports/sound_plan.json names the cut it was made for) and reused for it; a cut approved before it had one
    keeps no effects; with no sounds in the library nothing is decided (a later run may have them). `extra_db`:
    the owner's change to the music, which the effects follow."""
    sounds = library_sounds(feed_entries)
    own = Path(root) / "cache" / "sfx"
    have = set(sounds) | ({p.stem for p in own.glob("*")} if own.exists() else set())
    mark = cut_mark(shots)
    plan_path = Path(root) / "reports" / "sound_plan.json"
    try:
        kept = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        kept = None
    if isinstance(kept, dict) and kept.get("cut") == mark and isinstance(kept.get("events"), list):
        planned = kept["events"]                         # decided for this very cut: the same every render
    elif any((Path(root) / "reviews" / f"{g}.approved").exists() for g in APPROVED_CUT):
        planned = []                                     # approved before sound effects: the film stays as approved
    elif not have:
        return []                                        # nothing to choose from: decided when there is
    else:
        planned = plan_sound(scenes, shots, plans, have)
    copies = film_copies(root, {p["name"]: sounds.get(p["name"], Path("/nonexistent")) for p in planned})
    events = []
    for p in planned:
        src = copies.get(p["name"])
        if src is None:
            continue
        try:
            gain = p["gain_db"] + _level(src) + extra_db
            if p["kind"] == "bed":
                src = looped(src, p["duration_s"], Path(work) / f"bed_{p['scene_id']}.m4a")
            events.append((src, p["start_s"], round(gain, 2)))
        except Exception as exc:                         # one sound that will not play never takes the rest
            log(f"  sound effect {p['name']} skipped: {type(exc).__name__}: {str(exc)[:100]}")
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({"cut": mark, "events": planned}, indent=1), encoding="utf-8")
    beds, cuts = sum(p["kind"] == "bed" for p in planned), sum(p["kind"] == "cut" for p in planned)
    if planned:
        log(f"  sound effects: {beds} scene ambience, {cuts} on cuts")
    return events
