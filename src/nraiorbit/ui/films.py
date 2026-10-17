"""What the control room shows about a film, derived from the files the pipeline and the runner write.

Nothing here is a second copy of state: every status is read from the plan files, the stage folders, the reports
and the queue row, so the page can never disagree with what the runner sees.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

import yaml

from .store import Store

STAGES = [
    ("story", "Story bible"), ("outline", "Outline"), ("scenes", "Scenes & dialogue"), ("cast", "Cast"),
    ("shots", "Shot lists"), ("faces", "First frames"), ("render", "Picture, voices, music, lips"),
    ("assemble", "Film & trailer"), ("qa", "QA"), ("deliver", "Delivery"), ("publish", "YouTube"),
]
GATES = {"story": "story", "cast": "cast", "faces": "faces", "pacing": "assemble", "final_cut": "assemble",
         "trailer": "assemble"}
NOT_FILMS = {"queue", "library", "lipsync", "stories", "_template", "final"}


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
    gates = [n.rsplit("/", 1)[-1][:-len(".pending")] for n in store.list(f"{slug}/reviews/") if n.endswith(".pending")]
    state = (produce or {}).get("state") or (row or {}).get("state") or ""
    if state.startswith("awaiting:") and state[9:] not in gates:
        gates.append(state[9:])
    return gates


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
        "note": (row or {}).get("note"), "produce": produce,
    }


def detail(store: Store, slug: str, row: dict | None) -> dict:
    """Everything the film page shows."""
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
    put("faces", "awaiting" if "faces" in gates else ("done" if with_leads and framed >= len(with_leads)
                                                      else ("partial" if framed else "todo")),
        f"{framed} of {len(with_leads)} shots with a lead have a first frame" if with_leads else "")
    filmed = sum(1 for sh in shots if sh.get("shot_id") in clips)
    cues = [c for c in cue_sheet.get("cues", []) if c.get("instruments")]
    render_bits = []
    if shots:
        render_bits.append(f"{filmed} of {len(shots)} shots filmed")
    if cues:
        render_bits.append(f"{len(music_files)} of {len(cues)} music cues")
    if lipsync:
        render_bits.append(f"{speaking_needed} shots lip-synced")
    render_status = "done" if steps.get("render") == "done" or (shots and filmed >= len(shots) and master) else (
        "partial" if filmed or music_files else "todo")
    if str(steps.get("render", "")).startswith("blocked") or str(steps.get("render", "")).startswith("estimate"):
        render_status = "blocked"
    put("render", render_status, "; ".join(render_bits))
    has_film = any(n.endswith("/movie.mp4") for n in master)
    has_trailer = any(n.endswith("/trailer.mp4") for n in master)
    put("assemble", "awaiting" if any(g in gates for g in ("pacing", "final_cut", "trailer")) else
        ("done" if has_film else "todo"), ", ".join(x for x, ok in (("film", has_film), ("trailer", has_trailer)) if ok))
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
        pub if isinstance(pub, str) else "")

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
        "money": money(store, slug, cfg, row, produce), "produce": produce, "steps": steps,
        "review_queue": review_queue[-30:], "qa_film": qa_film, "qa_md": qa_md,
        "strips": [{"shot_id": n.rsplit("/", 1)[-1].replace("_strip.jpg", ""), "url": img(n)} for n in strips],
        "media": media, "delivery": deliver_links, "title": bible.get("title_en") or slug, "title_te": bible.get("title_te"),
        "state": (row or {}).get("state") or (produce or {}).get("state") or ("made by hand" if bible else "new"),
        "policy": cfg.get("policy") or {}, "runtime": (cfg.get("film") or {}).get("runtime_minutes"),
        "story_text": store.read_text(f"{slug}/project/input/story.md") or "",
    }
