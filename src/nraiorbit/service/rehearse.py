"""What stands between a film and its YouTube upload, all at once and for free (`nraiorbit rehearse <slug>`, and the
runner after every run, into reports/rehearsal.json for the control room).

The runner meets the stops one tick at a time: characters without pictures, speakers without voices, a shot plan the
video model bills badly, an estimate over the cap. A rehearsal asks the runner's own checks together against the
film's plan as it stands, so nothing here can disagree with what a run will do. It makes no paid call and writes
nothing but its report. Three kinds of finding:

    stops   a run will stop here until the owner changes something (each with the change)
    waste   a run will go on, but pays for something it throws away (each with how to avoid it)
    notes   what the runner does next on its own, and the gates that wait for the owner
"""

from __future__ import annotations

import datetime
import json

from ..schemas import Outline, SceneScript, ShotList, StoryBible
from .queue import GATES as GATE_ORDER


def _money(x: float) -> str:
    return f"${x:,.2f}"


def rehearse(pipeline) -> dict:
    """Every finding for this film's next runs: {"stops": [...], "waste": [...], "notes": [...], "estimate": ...};
    each finding is {"step", "what", "fix"}, `step` a control-room stage key."""
    from ..costing import veo_clip_seconds, veo_durations, veo_settings
    from ..identity import _front_of, is_animal
    from ..pipeline import STEP_GATES
    from .produce import COST_GROWTH, _faces_needed, approved, approved_figure, cost_gate, policy_of, uploaded_already

    p, cfg = pipeline.p, pipeline.cfg
    prov = cfg.get("providers") or {}
    out: dict = {"stops": [], "waste": [], "notes": [], "estimate": None,
                 "checked": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}

    def add(kind, step, what, fix=""):
        out[kind].append({"step": step, "what": what, "fix": fix})

    # 1. planning: what Claude still has to write (planning is cheap and runs on its own)
    bible = StoryBible.model_validate_json(p.bible.read_text(encoding="utf-8")) if p.bible.exists() else None
    outline = Outline.model_validate_json(p.outline.read_text(encoding="utf-8")) if p.outline.exists() else None
    scene_ids = [sc.scene_id for sc in outline.scenes] if outline else []
    unplanned = [label for label, done in (
        ("story bible", bible is not None), ("outline", outline is not None),
        ("scenes", bool(scene_ids) and all((p.scenes / f"{s}.json").exists() for s in scene_ids)),
        ("cast", p.lock.exists()),
        ("shot lists", bool(scene_ids) and all((p.shots / f"{s}.json").exists() for s in scene_ids))) if not done]
    if unplanned:
        add("notes", "story", f"not planned yet: {', '.join(unplanned)}; Claude writes {'it' if len(unplanned) == 1 else 'them'} "
                              "on the next run, and the checks below see more once it has")
    specs = {c.id: c for c in bible.characters} if bible else {}
    lock = pipeline._lock() if p.lock.exists() else None
    shots = [s for sid in scene_ids if (p.shots / f"{sid}.json").exists()
             for s in ShotList.model_validate_json((p.shots / f"{sid}.json").read_text(encoding="utf-8")).shots]
    blocks = {b.block_id: b for sid in scene_ids if (p.scenes / f"{sid}.json").exists()
              for b in SceneScript.model_validate_json((p.scenes / f"{sid}.json").read_text(encoding="utf-8")).blocks}
    image_price = float((pipeline.pricing.get("gemini") or {}).get("image_per_image", 0.04))

    # 2. the cast's reference pictures (a film that makes first frames or video needs one set per character)
    makes_pictures = any(prov.get(k, "mock") not in ("mock", "none") for k in ("keyframe", "video"))
    if lock is not None and makes_pictures:
        new = [c for c, e in lock.characters.items() if e.character_asset.endswith("@pending")]
        bare = []
        for cid, entry in lock.characters.items():
            if entry.character_asset.endswith("@pending"):
                continue
            try:
                if not _front_of(pipeline.library, pipeline.library.resolve(entry.character_asset)).exists():
                    bare.append(cid)
            except (KeyError, ValueError):
                bare.append(cid)
        if new or bare:
            pictures = 3 * (len(new) + len(bare))
            if (cfg.get("cast") or {}).get("build_identity"):
                add("notes", "cast", f"the cast step makes reference pictures for {', '.join(new + bare)}: about "
                                     f"{pictures} pictures, {_money(pictures * image_price)}")
            else:
                add("stops", "cast", f"no reference pictures for {', '.join(new + bare)}: video is never bought "
                                     "without them", "add `cast: {build_identity: true}` to the film's pipeline.yaml "
                                                     "(the cast step then makes them), or build them with "
                                                     "`nraiorbit identity build`")

    # 3. voices: every line ElevenLabs speaks needs a voice for its speaker
    v = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
    to_elevenlabs = prov.get("tts") == "elevenlabs" or (prov.get("tts") == "inbox" and v.get("inbox_fallback") == "elevenlabs")
    animals = {c for c, spec in specs.items() if is_animal(spec)}
    if to_elevenlabs and blocks and lock is not None:
        voiceless = pipeline._voiceless()
        beasts = [c for c in voiceless if c in animals]
        people = [c for c in voiceless if c not in animals]
        if people:
            if v.get("auto_cast") and not (p.reports / "voices.json").exists():
                add("notes", "cast", f"the cast step picks ElevenLabs voices for {', '.join(people)} from your account")
            else:
                add("stops", "render", f"no ElevenLabs voice for {', '.join(people)}",
                    "set one on each cast card (or turn on voice.auto_cast to have the runner pick)")
        if beasts:
            add("stops", "render", f"{', '.join(beasts)} {'is an animal' if len(beasts) == 1 else 'are animals'} with "
                                   "lines and no voice: ElevenLabs would read the chatter description aloud",
                "set a voice on the cast card, put recordings in inbox/dialogue/<line id>, or rewrite the lines as sound")

    # 4. the shot plan against the clip lengths the video model bills
    if prov.get("video") == "veo" and shots:
        g = cfg.get("google") or {}
        mode = g.get("veo_mode", "keyframe")
        lengths = set(veo_durations(pipeline.pricing, veo_settings(g)[2]))
        billed = {s.shot_id: veo_clip_seconds(s.duration_s, pipeline.pricing, veo_settings(g, s)[2], mode,
                                              bool(s.characters_in_frame)) for s in shots}
        trimmed = sum(max(0.0, billed[s.shot_id] - s.duration_s) for s in shots)    # paid, then cut away
        short = [s for s in shots if billed[s.shot_id] - s.duration_s > 0.05]
        if short and trimmed > 1:
            grid = (cfg.get("shots") or {}).get("billable_grid")
            add("waste", "shots", f"{len(short)} of {len(shots)} shots are shorter than the "
                                  f"{'/'.join(str(x) for x in sorted(lengths))} s clips Veo bills: {trimmed:.0f} s of "
                                  "generated video paid for and cut away",
                "re-plan the shots" + ("" if grid else " with `shots: {billable_grid: true}` in the film's pipeline.yaml"))
        longest = max(lengths)
        held = [s for s in shots if s.duration_s > longest + 0.05 and not (mode == "reference" and s.characters_in_frame)]
        if held:
            add("notes", "shots", f"{len(held)} shots run longer than Veo's {longest} s clip "
                                  f"({', '.join(s.shot_id for s in held[:6])}{'…' if len(held) > 6 else ''}): the rest of "
                                  "each is held or slowed in the edit", "re-plan the shots on the clip grid")

    # 5. the cost gate, exactly as the run will ask it
    if shots:
        block, preview = cost_gate(pipeline)
        cap = (cfg.get("budget") or {}).get("max_usd")
        if preview:
            out["estimate"] = {k: round(float(preview.get(k) or 0), 2) for k in
                               ("total_usd", "video_usd", "keyframe_usd", "tts_usd", "music_usd", "lipsync_usd")}
        if block:
            add("stops", "cost", block["why"], "raise the cap under costs on the film page, or reduce the plan")
        elif preview and preview.get("total_usd"):
            e = out["estimate"]
            parts = ", ".join(f"{name} {_money(e[k])}" for name, k in (("video", "video_usd"), ("first frames", "keyframe_usd"),
                                                                       ("voices", "tts_usd"), ("music", "music_usd"),
                                                                       ("lip-sync", "lipsync_usd")) if e[k])
            add("notes", "cost", f"the paid stages: about {_money(e['total_usd'])} ({parts}), cap "
                                 f"{_money(float(cap)) if cap else 'none'}, {_money(pipeline.ledger.spent())} spent "
                                 "(an upper bound: work already made may cost less)")

    # 6. lip-sync on an animal's face: paid, and nothing to sync
    if prov.get("lipsync") == "sync_so" and shots and animals:
        mouths = [s for s in shots if any(blocks.get(b) is not None and blocks[b].kind == "dialogue"
                                          and blocks[b].character_id in animals
                                          and blocks[b].character_id in s.characters_in_frame for b in s.block_ids)]
        if mouths:
            ids = ", ".join(s.shot_id for s in mouths)
            add("waste", "render", f"lip-sync would run on an animal's face in {len(mouths)} shots: {ids}",
                "in the film's pipeline.yaml set `lipsync: {shots: {<shot id>: {sync: false}}}` for each of them, or "
                "rewrite the animals' lines as sound")

    # 7. what waits for the owner
    pol = policy_of(cfg)
    animatic = pipeline._animatic()
    total = float((out["estimate"] or {}).get("total_usd") or 0)
    gates = cfg.get("review_gates") or {}
    waiting = []
    for gate in GATE_ORDER:
        if gate == "cost":                                # produce's own rule: something to pay, not approved yet,
            if total > 0:                                 # or grown since the approval
                was = approved_figure(pipeline, "cost") if approved(pipeline, "cost") else None
                if not approved(pipeline, "cost") or (was is not None and total > was * (1 + COST_GROWTH) + 0.5):
                    waiting.append(gate)
            continue
        if gate == "publish":                             # until this very cut is uploaded (the approval is spent there)
            movie = p.master / "movie.mp4"
            record = json.loads((p.reports / "youtube.json").read_text(encoding="utf-8")) \
                if (p.reports / "youtube.json").exists() else {}
            up = (record.get("film") or {}) if isinstance(record.get("film"), dict) else {}
            same_size = movie.exists() and (up.get("file") or {}).get("bytes") == movie.stat().st_size
            if pol["publish"] in ("private", "unlisted", "public") and not approved(pipeline, "publish") and \
                    not (same_size and uploaded_already(record, "film", movie)):   # the size first: no hashing
                waiting.append(gate)                                               # a new cut of another size
            continue
        if approved(pipeline, gate) or gate == ("final_cut" if animatic else "pacing") or pol["gates"] != "ask":
            continue                                      # a film meets the pacing gate or the final cut, not both
        if gate == "faces":
            if _faces_needed(pipeline):
                waiting.append(gate)
            continue
        if gate in STEP_GATES and gate not in gates:      # as Pipeline.gate: a step gate the project never asked for
            continue
        g = gates.get(gate) or {}
        if g.get("enabled", True) and not g.get("auto_approve", False):
            waiting.append(gate)
    if waiting:
        add("notes", "publish", f"waits for you at: {', '.join(waiting)}")
    if pol["publish"] not in ("private", "unlisted", "public"):
        add("notes", "publish", "no YouTube upload: publish is none for this film")
    return out


def save(pipeline, log=print) -> dict | None:
    """The rehearsal into reports/rehearsal.json; never a run's end: a failure is written down instead."""
    try:
        found = rehearse(pipeline)
    except Exception as exc:                             # a half-planned project, a stand-in in a test
        found = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    try:
        pipeline.p.reports.mkdir(parents=True, exist_ok=True)
        (pipeline.p.reports / "rehearsal.json").write_text(json.dumps(found, indent=1, ensure_ascii=False),
                                                           encoding="utf-8")
    except Exception as exc:
        log(f"  rehearsal not written: {type(exc).__name__}")
        return None
    return found


def report_text(found: dict) -> str:
    """The rehearsal for a terminal: stops first, then waste, then notes."""
    if found.get("error"):
        return f"rehearsal failed: {found['error']}"
    lines = []
    for kind, title in (("stops", "Stops (a run waits here until you change something)"),
                        ("waste", "Waste (a run goes on but pays for what it throws away)"), ("notes", "Notes")):
        if found.get(kind):
            lines.append(title)
            for f in found[kind]:
                lines.append(f"  [{f['step']}] {f['what']}" + (f"\n      → {f['fix']}" if f.get("fix") else ""))
    return "\n".join(lines) or "Nothing stands in the way: the next runs go through to the gates that wait for you."
