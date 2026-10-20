"""What the control room shows about a film, derived from the files the pipeline and the runner write.

Nothing here is a second copy of state: every status is read from the plan files, the stage folders, the reports
and the queue row, so the page can never disagree with what the runner sees.
"""

from __future__ import annotations

import datetime
import json
from collections import Counter, defaultdict

import yaml

from .store import Store

STAGES = [
    ("story", "Story bible"), ("outline", "Outline"), ("scenes", "Scenes & dialogue"), ("cast", "Cast"),
    ("shots", "Shot lists"), ("cost", "Cost"), ("faces", "First frames"), ("render", "Picture, voices, music, lips"),
    ("assemble", "Film & trailer"), ("qa", "QA"), ("deliver", "Delivery"), ("publish", "YouTube"),
]
GATES = {"story": "story", "outline": "outline", "scenes": "scenes", "cast": "cast", "shots": "shots", "cost": "cost",
         "faces": "faces", "render": "render", "pacing": "assemble", "final_cut": "assemble", "trailer": "assemble",
         "publish": "publish"}
# the runner's phase names (pipeline stages and produce steps) -> the stage the page draws them on; restore, backup
# and the library phases have no stage and show in the headers only
PHASES = {"story_bible": "story", "outline": "outline", "scenes": "scenes", "cast": "cast", "shots": "shots",
          "music_plan": "render", "render": "render", "assemble": "assemble", "trailer": "assemble", "cost": "cost",
          "faces": "faces", "qa": "qa", "deliver": "deliver", "publish": "publish"}
RUNNING = {"new", "approved", "retry", "producing"}       # row states in which the runner is, or is about to be, on it
SETTLED = {"done", "skipped", "held"}                     # statuses that let the next step count


def _base_providers() -> dict:
    """The providers the base config supplies for whatever a project leaves out (all mock, today)."""
    try:
        from ..config import load_pipeline_config
        return dict(load_pipeline_config(None).get("providers") or {})
    except Exception:
        return {}


BASE_PROVIDERS = _base_providers()
ADVISORY = {"qa"}                                         # steps that never stop the run: no later step waits on them


def animatic(cfg: dict) -> bool:
    """A film whose providers are all mock or none runs as an animatic: placeholder picture, silent voices, mock
    music, nothing paid (the pipeline's own rule, with the base config's defaults for what the project leaves out).
    A film queued without a template looks exactly like this: its pipeline.yaml names no providers."""
    prov = {**BASE_PROVIDERS, **(cfg.get("providers") or {})}
    return all(prov.get(k, "mock") in ("mock", "none") for k in ("keyframe", "video", "tts", "music"))


def in_order(stage: dict) -> None:
    """A later step never counts as done while an earlier one is open: a cut that exists before every shot is filmed
    is from an earlier run (an animatic, a previous take) and is remade after the open step, so it shows as stale.
    QA is advisory, so a flagged or skipped check opens nothing: the delivery it sits before is real."""
    first_open = None
    for key, label in STAGES:
        st = stage[key]["status"]
        if key in ADVISORY:
            continue
        if first_open is None and st not in SETTLED:
            first_open = label.lower()
        elif first_open is not None and st == "done":
            stage[key] = {"status": "stale", "text": f"from an earlier run; redone after {first_open}"}
NOT_FILMS = {"queue", "library", "lipsync", "stories", "_template", "final"}


def when(iso) -> datetime.datetime | None:
    """A tz-aware moment from an ISO string; a naive one is UTC, as the runner and Cloud Run write them. None when
    the text is not a time, so a template never breaks on a half-written record."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def seconds_since(iso, now=None) -> float | None:
    """Never negative: a clock a little ahead on the runner must not read as "in the future"."""
    t = when(iso)
    if t is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (now - t).total_seconds())


def ago(iso, now=None) -> str:
    """"40 s ago", "12 min ago", "3 h ago": how old a timestamp is, for telling fresh from stale at a glance."""
    secs = seconds_since(iso, now)
    if secs is None:
        return ""
    secs = int(secs)
    if secs < 60:
        return f"{secs} s ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 2 * 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def clock(iso, tz=None) -> str:
    """"03:05 EDT" for an ISO time on the studio's clock (`tz`; UTC when none), "" when it cannot be read."""
    t = when(iso)
    if t is None:
        return ""
    t = t.astimezone(tz or datetime.timezone.utc)
    return f"{t.strftime('%H:%M')} {t.tzname() or ''}".strip()


def live_progress(record, running, tz=None) -> dict | None:
    """The progress record as the page shows it, only while its execution is still running: a finished record, or
    one left behind by a crash, must never mark a stage as under way. Adds the stage it lands on, the clock time its
    phase began and how long ago it last spoke."""
    if not isinstance(record, dict) or record.get("state") != "running" or record.get("execution") not in running:
        return None
    phase = str(record.get("phase") or "")
    key = PHASES.get(phase)
    return {**record, "stage_key": key, "stage_label": dict(STAGES).get(key) or phase.replace("_", " "),
            "since": clock(record.get("phase_started") or record.get("started"), tz), "ago": ago(record.get("updated"))}


def _json(store: Store, rel: str):
    text = store.read_text(rel)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _yaml(store: Store, rel: str):
    text = store.read_text(rel)
    if not text:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _jsonl(store: Store, rel: str) -> list[dict]:
    text = store.read_text(rel) or ""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def film_slugs(store: Store, rows: list[dict]) -> list[str]:
    """Films in the queue first (in sheet order), then folders in the store that hold a plan but no row."""
    seen = [r["slug"] for r in rows if r.get("slug")]
    for name in store.dirs():
        if name not in seen and name not in NOT_FILMS and not name.startswith("."):
            if store.exists(f"{name}/project/pipeline.yaml") or store.exists(f"{name}/project/01_story_bible.json"):
                seen.append(name)
    return seen


def money(store: Store, slug: str, cfg: dict, row: dict | None, produce: dict | None) -> dict:
    costs = _jsonl(store, f"{slug}/reports/cost.jsonl")
    by = Counter()
    for c in costs:
        by[c.get("provider", "?")] += float(c.get("usd") or 0)
    spent = round(sum(by.values()), 2)
    cap = (cfg.get("budget") or {}).get("max_usd")
    try:
        cap = float(cap) if cap not in (None, "") else None           # a hand-edited "40" is still a cap
    except (TypeError, ValueError):
        cap = None
    if row and row.get("budget_usd"):
        try:
            cap = float(row["budget_usd"])
        except ValueError:
            pass
    est = ((produce or {}).get("steps") or {}).get("estimate") if produce else None
    return {"spent": spent, "cap": cap, "by_provider": sorted(by.items(), key=lambda t: -t[1]), "items": len(costs),
            "estimate": est if isinstance(est, dict) else None,
            "left": round(float(cap) - spent, 2) if cap else None}


def pending_gates(store: Store, slug: str, produce: dict | None, row: dict | None) -> list[str]:
    """What waits on the owner. A film in the queue waits only when its row says so (awaiting:<gate>): while a run
    is on it the bucket still holds the previous stop's report and marker, and those must never offer a button. A
    film driven by hand has no row: its markers never approved are what wait (a marker with an .approved sibling is
    spent; the bucket mirror can bring one back)."""
    if row is not None:
        state = row.get("state") or ""
        return [state[len("awaiting:"):]] if state.startswith("awaiting:") else []
    names = {n.rsplit("/", 1)[-1] for n in store.list(f"{slug}/reviews/")}
    state = (produce or {}).get("state") or ""
    waiting = state[9:] if state.startswith("awaiting:") else None
    gates = [n[:-len(".pending")] for n in sorted(names) if n.endswith(".pending")
             and (n[:-len(".pending")] == waiting or f"{n[:-len('.pending')]}.approved" not in names)]
    if waiting and waiting not in gates:
        gates.append(waiting)
    if isinstance(((produce or {}).get("steps") or {}).get("publish"), dict):     # uploaded: that marker is spent
        gates = [g for g in gates if g != "publish"]
    return gates


def explain_note(note: str | None) -> str | None:
    """A plain sentence for the runner's blocked notes that have one fix."""
    text = note or ""
    if "Could not resolve authentication method" in text or "ANTHROPIC_API_KEY" in text:
        return ("The runner has no Anthropic API key, so the story and screenplay agents cannot sign in to Claude. "
                "Store a key as the Secret Manager secret ANTHROPIC_API_KEY, attach it to the runner job, then Retry.")
    if "insufficient_credits" in text or "402" in text:
        return "A paid service answered 402: its prepaid credit is used up. Top it up, then Retry."
    return None


def progress_record(store: Store, slug: str) -> dict | None:
    """The runner's progress record for the film as the bucket holds it: None when there is none or it is not JSON."""
    rec = _json(store, f"{slug}/reports/progress.json")
    return rec if isinstance(rec, dict) else None


def blocked_issue(state: str, note: str | None, progress: dict | None, stage: dict) -> dict | None:
    """Which step a blocked film failed on, for its card: the runner's own last phase when its progress record ended
    blocked or failed, else the first step still open. The text is the row's note (the error), or the reason."""
    if not str(state).startswith("blocked:"):
        return None
    key = None
    if isinstance(progress, dict) and progress.get("state") in ("blocked", "failed"):
        key = PHASES.get(str(progress.get("phase") or ""))
    if key is None:
        key = next((k for k, _ in STAGES if k not in ADVISORY and stage[k]["status"] not in SETTLED), "publish")
    reason = state[len("blocked:"):]
    text = (note or reason).strip()
    return {"stage": key, "label": dict(STAGES)[key], "reason": reason, "note": note or "",
            "text": text if len(text) <= 160 else text[:157] + "…", "hint": explain_note(note)}


def cost_lines(estimate: dict | None, cfg: dict, to_film: list, to_frame: list, scenes: list, cues: list) -> list[dict]:
    """The money analysis on the Cost step: what each paid part of the next run costs and why. The counts are what
    the estimate prices: shots still to film (clips in the inbox are reused), frames still to make, dialogue lines.
    Video carries the seconds Veo bills against the seconds on screen, since a shot is billed at the next clip
    length up (4, 6 or 8 s, as produce prices it): a film cut in 1.7-second shots pays for more than twice its length."""
    if not isinstance(estimate, dict):
        return []
    from .. import costing
    try:
        if not to_film:
            raise LookupError("nothing to film: the parts without the seconds")
        pricing = costing.load_pricing()
        google = cfg.get("google") or {}
        res = str(google.get("veo_resolution") or pricing["veo"].get("resolution") or "720p")
        mode = str(google.get("veo_mode") or "keyframe")
        shortest = min(costing.veo_durations(pricing, res))
        billed = sum(costing.veo_clip_seconds(float(sh.get("duration_s") or 0), pricing, res, mode,
                                              bool(sh.get("characters_in_frame"))) for sh in to_film)
    except Exception:                                     # a pricing file off this machine: the parts still show
        billed = None
    screen = sum(float(sh.get("duration_s") or 0) for sh in to_film)
    lines = sum(1 for sc in scenes for b in (sc.get("blocks") or []) if b.get("kind") == "dialogue")
    video_why = f"{len(to_film)} shots to film" if to_film else ""
    if billed is not None:
        video_why += f" · {billed:.0f} s billed for {screen:.0f} s on screen"
        if screen and billed > screen * 1.25:
            video_why += f" ({100 * (billed - screen) / billed:.0f}% of the video spend is trimmed away: shots " \
                         f"shorter than the {shortest} s clip)"
    parts = [("video", "video_usd", video_why),
             ("first frames", "keyframe_usd", f"{len(to_frame)} frames to make" if to_frame else ""),
             ("voices", "tts_usd", f"{lines} line{'s' if lines != 1 else ''}" if lines else ""),
             ("music", "music_usd", f"{len(cues)} cues" if cues else ""),
             ("lip sync", "lipsync_usd", "")]
    out = [{"name": name, "usd": float(estimate.get(k) or 0), "why": why}
           for name, k, why in parts if estimate.get(k) not in (None, 0, 0.0)]
    return out


def summary(store: Store, slug: str, row: dict | None) -> dict:
    """The board row: title, state, gates, money. Cheap: a handful of reads."""
    cfg = _yaml(store, f"{slug}/project/pipeline.yaml") or {}
    bible = _json(store, f"{slug}/project/01_story_bible.json")
    produce = _json(store, f"{slug}/reports/produce.json")
    master = store.list(f"{slug}/11_master/")
    gates = pending_gates(store, slug, produce, row)
    state = (row or {}).get("state") or (produce or {}).get("state") or ("made by hand" if bible else "new")
    return {
        "slug": slug, "row": row, "title": (bible or {}).get("title_en") or slug, "title_te": (bible or {}).get("title_te"),
        "logline": (bible or {}).get("logline"), "state": state, "gates": gates,
        "runtime": (cfg.get("film") or {}).get("runtime_minutes"),
        "money": money(store, slug, cfg, row, produce),
        "has_film": any(n.endswith("/movie.mp4") for n in master),
        "has_trailer": any(n.endswith("/trailer.mp4") for n in master),
        "updated": (row or {}).get("updated") or (produce or {}).get("finished"),
        "note": (row or {}).get("note"), "hint": explain_note((row or {}).get("note")), "produce": produce,
        "animatic": animatic(cfg),
    }


def detail(store: Store, slug: str, row: dict | None, running=(), tz=None) -> dict:
    """Everything the film page shows. `running` is the set of execution names Cloud Run reports as running: the
    runner's progress record counts as live only while its execution is among them."""
    cfg = _yaml(store, f"{slug}/project/pipeline.yaml") or {}
    bible = _json(store, f"{slug}/project/01_story_bible.json") or {}
    outline = _json(store, f"{slug}/project/02_outline.json") or {}
    cast = _yaml(store, f"{slug}/project/cast.lock.yaml") or {}
    produce = _json(store, f"{slug}/reports/produce.json")
    names = {c["id"]: c for c in bible.get("characters", [])}
    scenes_out = outline.get("scenes", [])

    scene_files = [n for n in store.list(f"{slug}/03_scenes/") if n.endswith(".json")]
    scenes = [s for s in (_json(store, n) for n in scene_files) if s]
    shot_files = [n for n in store.list(f"{slug}/04_shots/") if n.endswith(".json")]
    shot_lists = [s for s in (_json(store, n) for n in shot_files) if s]
    shots = [sh for sl in shot_lists for sh in sl.get("shots", [])]

    keyframes = defaultdict(list)
    for n in store.list(f"{slug}/06_keyframes/"):
        if n.endswith((".png", ".jpg")):
            keyframes[n.split("/")[2]].append(n)
    clips = {n.split("/")[-1].split(".")[0].split("_take")[0] for n in store.list(f"{slug}/inbox/video/")
             if n.endswith(".mp4")}
    music_files = [n for n in store.list(f"{slug}/08_music/") if n.endswith(".wav")]
    cue_sheet = _json(store, f"{slug}/08_music/cue_sheet.json") or {}
    master = store.list(f"{slug}/11_master/")
    lipsync = _jsonl(store, f"{slug}/reports/lipsync.jsonl")
    synced = {r["shot_id"] for r in lipsync if r.get("mode") and r["mode"] != "not synced"}
    review_queue = _jsonl(store, f"{slug}/reports/review_queue.jsonl")
    qa_film = _json(store, f"{slug}/reports/qa/film.json")
    qa_lip = _json(store, f"{slug}/reports/qa/lipsync/summary.json")
    qa_md = store.read_text(f"{slug}/reports/qa/lipsync/summary.md")
    strips = [n for n in store.list(f"{slug}/reports/qa/lipsync/") if n.endswith("_strip.jpg")]
    delivery = _json(store, f"{slug}/11_master/deliver/delivery.json")
    gates = pending_gates(store, slug, produce, row)
    steps = (produce or {}).get("steps") or {}
    cash = money(store, slug, cfg, row, produce)
    state = (row or {}).get("state") or (produce or {}).get("state") or ("made by hand" if bible else "new")

    with_leads = [sh for sh in shots if sh.get("characters_in_frame")]
    speaking_needed = len(synced)
    stage: dict[str, dict] = {}

    def put(key, status, text=""):
        stage[key] = {"status": status, "text": text}

    put("story", "awaiting" if "story" in gates else ("done" if bible else "todo"),
        f"{len(bible.get('characters', []))} characters, {len(bible.get('acts', []))} acts" if bible else "")
    put("outline", "done" if scenes_out else "todo",
        f"{len(scenes_out)} scenes, {sum(s.get('target_seconds', 0) for s in scenes_out) / 60:.1f} min" if scenes_out else "")
    put("scenes", "done" if scenes and len(scenes) >= len(scenes_out) else ("partial" if scenes else "todo"),
        f"{len(scenes)} of {len(scenes_out)} written, "
        f"{sum(1 for s in scenes for b in s.get('blocks', []) if b.get('kind') == 'dialogue')} lines" if scenes else "")
    put("cast", "awaiting" if "cast" in gates else ("done" if cast.get("characters") else "todo"),
        f"{len(cast.get('characters', {}))} locked" if cast.get("characters") else "")
    put("shots", "done" if shot_lists and len(shot_lists) >= len(scenes_out) else ("partial" if shot_lists else "todo"),
        f"{len(shots)} shots, {sum(sh.get('duration_s', 0) for sh in shots) / 60:.1f} min" if shots else "")
    framed = sum(1 for sh in with_leads if sh.get("shot_id") in keyframes)
    filmed = sum(1 for sh in shots if sh.get("shot_id") in clips)
    all_filmed = bool(shots) and filmed >= len(shots)
    mock = animatic(cfg)
    est = cash["estimate"]
    cost_step = str(steps.get("cost") or "")
    if mock:
        cost_status = "skipped"
    elif cost_step in ("approved", "not needed") or filmed or steps.get("render") == "done":
        cost_status = "done"
    elif str(steps.get("render", "")).startswith("estimate") or str(steps.get("render", "")).startswith("the paid"):
        cost_status = "blocked"
    else:
        cost_status = "todo"
    cost_text = ("nothing to pay: mock providers" if mock else
                 f"about ${est['total_usd']:,.2f} for the paid stages" if est and est.get("total_usd") else
                 ("nothing to pay" if cost_step == "not needed" else ""))
    if cash["cap"] and not mock:
        cost_text += f"{' · ' if cost_text else ''}cap ${cash['cap']:,.2f}, ${cash['spent']:,.2f} spent"
    put("cost", cost_status, cost_text)
    if "faces" in gates:
        faces_status = "awaiting"
    elif mock:
        faces_status = "skipped"                        # no video is bought, so no frame guards anything
    elif with_leads and framed >= len(with_leads):
        faces_status = "done"
    elif all_filmed or steps.get("render") == "done":
        faces_status = "done"                           # the film was shot; a frame no longer gates anything
    else:
        faces_status = "partial" if framed else "todo"
    faces_text = f"{framed} of {len(with_leads)} shots with a lead have a first frame" if with_leads else ""
    if faces_status == "done" and with_leads and framed < len(with_leads):
        faces_text = f"{framed} first frames; {len(with_leads) - framed} shots were filmed without one"
    if mock:
        faces_text = "no first frames: mock video"
    put("faces", faces_status, faces_text)
    cues = [c for c in cue_sheet.get("cues", []) if c.get("instruments")]
    cue_ids = {c.get("cue_id") for c in cues}
    cues_made = sum(1 for n in music_files if n.rsplit("/", 1)[-1][:-4] in cue_ids)
    render_bits = []
    if shots:
        render_bits.append(f"{filmed} of {len(shots)} shots filmed")
    if cues:
        render_bits.append(f"{cues_made} of {len(cues)} music cues")
    if lipsync:
        render_bits.append(f"{speaking_needed} shots lip-synced")
    render_status = "done" if steps.get("render") == "done" or (all_filmed and master) else (
        "partial" if filmed or music_files else "todo")
    if str(steps.get("render", "")).startswith("blocked") or str(steps.get("render", "")).startswith("estimate"):
        render_status = "blocked"
    has_film = any(n.endswith("/movie.mp4") for n in master)
    has_trailer = any(n.endswith("/trailer.mp4") for n in master)
    if mock and render_status != "blocked":             # placeholder clips are made on the fly and never backed up
        render_status = "done" if has_film or str(steps.get("render", "")) not in ("", "todo") else render_status
        render_bits = ["placeholder picture and voices (mock providers)"] + [b for b in render_bits if "music" in b]
    put("render", render_status, "; ".join(render_bits))
    cut = ", ".join(x for x, ok in (("film", has_film), ("trailer", has_trailer)) if ok)
    put("assemble", "awaiting" if any(g in gates for g in ("pacing", "final_cut", "trailer")) else
        ("done" if has_film else "todo"), cut + (" (animatic)" if mock and has_film else ""))
    qa_bits = []
    if qa_film is not None:
        qa_bits.append("film check " + ("ok" if qa_film.get("ok") else "needs a look"))
    if qa_lip:
        rows = qa_lip if isinstance(qa_lip, list) else qa_lip.get("shots") or qa_lip.get("rows") or []
        flagged = sum(1 for r in rows if isinstance(r, dict) and r.get("flags"))
        qa_bits.append(f"lip-sync: {flagged} of {len(rows)} shots flagged")
    put("qa", ("done" if qa_film is None or qa_film.get("ok") else "partial") if (qa_film is not None or qa_lip) else "todo",
        "; ".join(qa_bits))
    put("deliver", "done" if delivery else "todo",
        f"{len(delivery)} files" if isinstance(delivery, list) else ("" if not delivery else "delivered"))
    pub = steps.get("publish")
    put("publish", "done" if isinstance(pub, dict) else ("held" if isinstance(pub, str) and pub.startswith("held")
                                                         else ("skipped" if isinstance(pub, str) and pub.startswith("skipped")
                                                               else "todo")),
        pub if isinstance(pub, str) and not pub.startswith("awaiting") else "")
    gate_for = {}                                             # stage key -> the gate waiting there
    for g in gates:
        key = GATES.get(g)
        if key in stage:
            stage[key]["status"] = "awaiting"
            gate_for[key] = g
    in_order(stage)
    progress = progress_record(store, slug)
    issue = blocked_issue(state, (row or {}).get("note"), progress, stage)
    if issue:
        stage[issue["stage"]] = {"status": "blocked", "text": issue["text"]}
    # the step the runner is on (or about to pick up): the first unfinished stage while nothing waits on the owner
    next_stage = None
    if state.split(":")[0] in RUNNING and not gates:
        next_stage = next((k for k, _ in STAGES if stage[k]["status"] in ("todo", "partial", "stale")), None)
    # the runner's own word beats that inference: while its execution runs, the phase it reports is the one under way
    live = live_progress(progress, running, tz)
    if live and live["stage_key"]:
        text = f"{live['phase'].replace('_', ' ')} since {live['since']}"
        if live.get("last_line"):
            text += f" · last: {live['last_line']} ({live['ago']})"
        stage[live["stage_key"]] = {"status": "executing", "text": text}
        gate_for.pop(live["stage_key"], None)
        next_stage = live["stage_key"]

    def img(rel):
        return store.url(rel)

    keyframe_cards = []
    for sh in shots:
        sid = sh.get("shot_id")
        if sid in keyframes:
            keyframe_cards.append({"shot_id": sid, "url": img(keyframes[sid][0]), "characters": sh.get("characters_in_frame"),
                                   "filmed": sid in clips, "synced": sid in synced, "duration": sh.get("duration_s")})
    cast_cards = []
    for cid, entry in (cast.get("characters") or {}).items():
        spec = names.get(cid, {})
        asset = entry.get("character_asset") if isinstance(entry, dict) else None
        refs = [n for n in store.list(f"library/characters/{asset}/") if n.endswith((".png", ".jpg"))] if asset else []
        locked = entry if isinstance(entry, dict) else {}
        cast_cards.append({"id": cid, "name": spec.get("name_en", cid), "name_te": spec.get("name_te"),
                           "role": spec.get("role"), "physical": spec.get("physical"),
                           "personality": spec.get("personality"), "arc": spec.get("arc"), "voice": spec.get("voice"),
                           "voice_asset": locked.get("voice_asset"), "costumes": locked.get("costumes"),
                           "images": [img(r) for r in refs[:4]]})
    for cid, spec in names.items():                       # characters not locked yet still show from the bible
        if not any(c["id"] == cid for c in cast_cards):
            cast_cards.append({"id": cid, "name": spec.get("name_en", cid), "name_te": spec.get("name_te"),
                               "role": spec.get("role"), "physical": spec.get("physical"),
                               "personality": spec.get("personality"), "arc": spec.get("arc"), "voice": spec.get("voice"),
                               "voice_asset": None, "costumes": None, "images": []})
    scene_views = []
    for sc in scenes:
        blocks = []
        for b in sc.get("blocks", []):
            who = names.get(b.get("character_id") or "", {})
            blocks.append({**b, "speaker": who.get("name_en") or b.get("character_id")})
        here = next((o for o in scenes_out if o.get("scene_id") == sc.get("scene_id")), {})
        scene_views.append({**sc, "blocks": blocks, "outline": here})
    shot_views = []
    for sl in shot_lists:
        shot_views.append({"scene_id": sl.get("scene_id"), "shots": [
            {**sh, "keyframe": img(keyframes[sh["shot_id"]][0]) if sh.get("shot_id") in keyframes else None,
             "filmed": sh.get("shot_id") in clips, "synced": sh.get("shot_id") in synced} for sh in sl.get("shots", [])]})
    media = {}
    for name in ("movie.mp4", "trailer.mp4"):
        rel = f"{slug}/11_master/{name}"
        if any(n == rel for n in master):
            media[name] = store.url(rel)
    deliver_links = []
    if isinstance(delivery, list):
        for rec in delivery:
            rel = rec.get("gs", "") or ""
            name = rec.get("name") or rel.rsplit("/", 1)[-1]
            key = None
            if rel.startswith("gs://") and store.is_gs and rel[5:].startswith(store.bucket_name + "/"):
                key = rel[5 + len(store.bucket_name) + 1:]
            deliver_links.append({"name": name, "url": store.url(key, filename=name) if key else rec.get("url"),
                                  "mb": rec.get("mb")})
    return {
        "slug": slug, "row": row, "cfg": cfg, "bible": bible, "outline": scenes_out, "cast": cast_cards,
        "scenes": scene_views, "shots": shot_views, "keyframes": keyframe_cards, "stages": stage, "gates": gates,
        "gate_for": gate_for, "next_stage": next_stage, "live": live, "animatic": mock, "issue": issue,
        "cost_lines": cost_lines(cash["estimate"], cfg, [sh for sh in shots if sh.get("shot_id") not in clips],
                                 [sh for sh in with_leads if sh.get("shot_id") not in keyframes], scenes, cues)
        if not mock else [],
        "money": cash, "produce": produce, "steps": steps,
        "review_queue": review_queue[-30:], "qa_film": qa_film, "qa_md": qa_md,
        "strips": [{"shot_id": n.rsplit("/", 1)[-1].replace("_strip.jpg", ""), "url": img(n)} for n in strips],
        "media": media, "delivery": deliver_links, "title": bible.get("title_en") or slug, "title_te": bible.get("title_te"),
        "state": state,
        "hint": explain_note((row or {}).get("note")),
        "policy": cfg.get("policy") or {}, "runtime": (cfg.get("film") or {}).get("runtime_minutes"),
        "story_text": store.read_text(f"{slug}/project/input/story.md") or "",
    }
