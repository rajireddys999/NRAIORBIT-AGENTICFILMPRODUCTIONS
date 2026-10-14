"""Bill estimate: what each provider will charge for a project, before and after generating.

Works from the real shot plan when it exists (exact shot count and durations), or from a
runtime alone (`nraiorbit estimate --runtime 105`). Actual spend is read from the Claude
usage log and from the Veo clips you drop into the project's inbox/video folder.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import DEFAULT_CONFIG_DIR, deep_merge, load_yaml
from .schemas import SceneScript, ShotList

LEVELS = ("low", "expected", "high")
AVG_SHOT_S = 4.5          # used when there is no shot plan yet
SCENE_S = 90
LINES_PER_SHOT = 0.25
VEO_MB_PER_S = 1.5        # 720p clip size, for storage


class BudgetExceeded(RuntimeError):
    pass


class SpendLedger:
    """Every paid call is written to reports/cost.jsonl; calls that would cross the budget are refused."""

    def __init__(self, path: Path, max_usd: float | None):
        self.path = path
        self.max_usd = max_usd

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def spent(self) -> float:
        return sum(r["usd"] for r in self.records())

    def check(self, projected_usd: float, what: str) -> None:
        if self.max_usd is None:
            return
        spent = self.spent()
        if spent + projected_usd > self.max_usd + 1e-9:
            raise BudgetExceeded(
                f"budget ${self.max_usd:.2f} reached: spent ${spent:.2f}, next call ({what}) costs ${projected_usd:.2f}. "
                "Raise budget.max_usd (or --max-spend) to continue; finished work is kept.")

    def record(self, provider: str, item: str, usd: float, **details) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"provider": provider, "item": item, "usd": round(usd, 4), **details}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def summary(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.records():
            out[r["provider"]] = out.get(r["provider"], 0.0) + r["usd"]
        return out


def load_pricing(project_dir: Path | None = None) -> dict:
    pricing = load_yaml(DEFAULT_CONFIG_DIR / "pricing.yaml")
    if project_dir is not None and (project_dir / "pricing.yaml").exists():
        pricing = deep_merge(pricing, load_yaml(project_dir / "pricing.yaml"))
    return pricing


def billed_seconds(duration: float, durations: list[int], extension: int) -> tuple[int, int]:
    """(seconds billed, clips generated) for one attempt at a shot of `duration` seconds."""
    for d in sorted(durations):
        if duration <= d + 1e-6:
            return d, 1
    longest = max(durations)
    extensions = math.ceil((duration - longest) / extension)
    return longest + extensions * extension, 1 + extensions


def veo_durations(pricing: dict, resolution: str) -> list[int]:
    """Clip lengths Veo accepts at this resolution (1080p is 8 s only)."""
    veo = pricing["veo"]
    return veo.get("durations_by_resolution", {}).get(resolution, veo["durations_s"])


def veo_clip_seconds(duration: float, pricing: dict, resolution: str, mode: str, has_leads: bool) -> int:
    """Seconds billed for one Veo generation of a shot (one clip, no extensions)."""
    veo = pricing["veo"]
    if mode == "reference" and has_leads:
        return veo.get("reference_clip_s", 8)
    durations = veo_durations(pricing, resolution)
    return min(billed_seconds(duration, durations, veo["extension_s"])[0], max(durations))


def lyria_price(pricing: dict, model: str) -> float:
    """USD per Lyria generation (pricing.yaml lyria.per_generation); an unlisted model costs as much as the dearest."""
    table = pricing.get("lyria", {}).get("per_generation") or {}
    return float(table.get(model, max(table.values(), default=0.50)))


def load_takes(root: Path) -> dict[str, int]:
    """Chosen attempt per shot (projects/<slug>/takes.yaml); missing shots use attempt 1."""
    import yaml

    path = root / "takes.yaml"
    return {k: int(v) for k, v in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items()} if path.exists() else {}


def approx_tokens(text: str) -> int:
    """~4 bytes per token: right for English JSON; Telugu (3 bytes/char) lands near 0.75 token/char."""
    return max(1, len(text.encode("utf-8")) // 4)


@dataclass
class PlanStats:
    title: str
    minutes: float
    scenes: int
    shots: list[tuple[str, float, bool, bool]]   # (shot_id, duration_s, needs_lipsync, has_lead_characters)
    lines: int
    from_plan: bool
    artifact_tokens: dict[str, int] = field(default_factory=dict)


def plan_stats(project_dir: Path | None, runtime_minutes: float | None = None) -> PlanStats:
    if project_dir is not None and (project_dir / "04_shots").exists() and any((project_dir / "04_shots").glob("*.json")):
        return _stats_from_plan(project_dir)
    minutes = runtime_minutes or 105
    n = int(round(minutes * 60 / AVG_SHOT_S))
    lines = int(round(n * LINES_PER_SHOT))
    shots = [(f"shot{i:04d}", AVG_SHOT_S, i < lines, i % 5 < 3) for i in range(n)]   # ~60% show a lead
    return PlanStats(f"{minutes:g}-minute film (no shot plan yet)", minutes, int(round(minutes * 60 / SCENE_S)),
                     shots, lines, from_plan=False)


def _stats_from_plan(root: Path) -> PlanStats:
    tokens: dict[str, int] = {}
    bible_path = root / "01_story_bible.json"
    title = json.loads(bible_path.read_text(encoding="utf-8"))["title_en"] if bible_path.exists() else root.name
    for name in ("01_story_bible.json", "02_outline.json"):
        if (root / name).exists():
            tokens[name] = approx_tokens((root / name).read_text(encoding="utf-8"))
    shots: list[tuple[str, float, bool, bool]] = []
    lines = 0
    scenes = sorted((root / "04_shots").glob("*.json"))
    for path in scenes:
        shot_list = ShotList.model_validate_json(path.read_text(encoding="utf-8"))
        tokens[f"shots/{path.stem}"] = approx_tokens(path.read_text(encoding="utf-8"))
        script_path = root / "03_scenes" / path.name
        speakers: dict[str, str | None] = {}
        if script_path.exists():
            raw = script_path.read_text(encoding="utf-8")
            tokens[f"scene/{path.stem}"] = approx_tokens(raw)
            script = SceneScript.model_validate_json(raw)
            speakers = {b.block_id: b.character_id for b in script.blocks if b.kind == "dialogue"}
            lines += len(speakers)
        for shot in shot_list.shots:
            on_screen = any(speakers.get(b) in shot.characters_in_frame for b in shot.block_ids if b in speakers)
            needs_lipsync = on_screen and shot.size not in ("EWS", "WS", "aerial", "insert")
            shots.append((shot.shot_id, shot.duration_s, needs_lipsync, bool(shot.characters_in_frame)))
    minutes = sum(d for _, d, _, _ in shots) / 60
    return PlanStats(title, minutes, len(scenes), shots, lines, from_plan=True, artifact_tokens=tokens)


# ---------------------------------------------------------------------------- Claude

def claude_api_estimate(stats: PlanStats, pricing: dict) -> dict[str, float]:
    """Simulate the agent calls (bible, outline, scene + critique, shots, music) with prompt caching."""
    from .agents.base import PROMPTS_DIR

    p = pricing["claude"]
    think = 1 + p.get("thinking_multiplier", 1.5)
    agent_prompts = [approx_tokens(f.read_text(encoding="utf-8")) for f in PROMPTS_DIR.glob("*.md")
                     if f.stem != "shared_house_style"]
    sys = approx_tokens((PROMPTS_DIR / "shared_house_style.md").read_text(encoding="utf-8")) \
        + sum(agent_prompts) // max(1, len(agent_prompts))
    t = stats.artifact_tokens
    n_scenes = max(1, stats.scenes)
    bible = t.get("01_story_bible.json", 6000)
    outline = t.get("02_outline.json", 120 * n_scenes)
    scene_tokens = [v for k, v in t.items() if k.startswith("scene/")] or [900] * n_scenes
    shot_tokens = [v for k, v in t.items() if k.startswith("shots/")] or [int(250 * len(stats.shots) / n_scenes)] * n_scenes

    cost = 0.0

    def call(fresh_in: int, cached_prefix: int, out: int, first: bool) -> float:
        prefix_rate = p["cache_write"] if first else p["cache_read"]
        return (fresh_in * p["input"] + cached_prefix * prefix_rate + out * think * p["output"]) / 1e6

    cost += call(sys + 1500, 0, bible, True)                                   # story bible
    cost += call(400, sys + bible, outline, True)                              # outline
    for i, s in enumerate(scene_tokens):                                       # scenes + critique + 30% revisions
        cost += call(200, sys + bible + outline, s, i == 0)
        cost += call(s, sys + bible, 400, i == 0)
        cost += 0.3 * call(s + 400, sys + bible + outline, s, False)
    for i, (s, sh) in enumerate(zip(scene_tokens, shot_tokens)):               # shot lists
        cost += call(s + 300, sys + bible + 600, sh, i == 0)
    cost += call(150 * n_scenes, sys + bible, 600, True)                       # music director
    return {"low": cost * 0.7, "expected": cost, "high": cost * 1.8}


def claude_actual(project_dir: Path | None, pricing: dict) -> tuple[float, int] | None:
    if project_dir is None:
        return None
    log = project_dir / "reports" / "llm_usage.jsonl"
    if not log.exists():
        return None
    p, total, calls = pricing["claude"], 0.0, 0
    for line in log.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        total += (r["input_tokens"] * p["input"] + r["output_tokens"] * p["output"]
                  + (r.get("cache_read_input_tokens") or 0) * p["cache_read"]
                  + (r.get("cache_creation_input_tokens") or 0) * p["cache_write"]) / 1e6
        calls += 1
    return total, calls


# ---------------------------------------------------------------------------- estimate

@dataclass
class Estimate:
    stats: PlanStats
    veo_mode: str
    veo_resolution: str
    veo_seconds: int                       # billed seconds for one attempt at every shot
    veo_clips: int                         # generations for one attempt (incl. extensions)
    video_options: dict[str, dict[str, float]]
    flow: dict[str, dict[str, float]]
    claude_api: dict[str, float]
    claude_billing: str
    claude_logged: tuple[float, int] | None
    lipsync: dict[str, float]
    lipsync_hours: dict[str, float]
    storage: dict[str, float]
    mac_hours: dict[str, float]
    actual_veo: dict | None
    keyframes: dict[str, float] = field(default_factory=dict)
    tts: dict[str, float] = field(default_factory=dict)
    identity_pack: float = 0.0
    ledger: dict | None = None

    def other_costs(self, level: str) -> float:
        claude = self.claude_logged[0] if self.claude_logged else (
            0.0 if self.claude_billing == "subscription" else self.claude_api[level])
        return (claude + self.lipsync[level] + self.storage[level] + self.keyframes.get(level, 0.0)
                + self.tts.get(level, 0.0) + self.identity_pack)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stats"] = {k: v for k, v in d["stats"].items() if k != "shots"} | {"shot_count": len(self.stats.shots)}
        return d


def gpu_cloud(pricing: dict) -> dict:
    """The cloud that runs lip-sync and stores media (`cloud:` in pricing.yaml; Google Cloud by default)."""
    return pricing[pricing.get("cloud", "gcp")]


def estimate(project_dir: Path | None = None, runtime_minutes: float | None = None,
             pricing: dict | None = None, cfg: dict | None = None) -> Estimate:
    from .config import load_pipeline_config

    pricing = pricing or load_pricing(project_dir)
    cfg = cfg or load_pipeline_config(project_dir)
    mode = cfg.get("google", {}).get("veo_mode", "keyframe")
    stats = plan_stats(project_dir, runtime_minutes)
    att = pricing["attempts_per_clip"]
    veo = pricing["veo"]
    res = cfg.get("google", {}).get("veo_resolution") or veo["resolution"]

    seconds = clips = 0
    for _, dur, _, leads in stats.shots:
        if mode == "reference" and leads:
            s, c = veo.get("reference_clip_s", 8), 1
        else:
            s, c = billed_seconds(dur, veo_durations(pricing, res), veo["extension_s"])
        seconds += s
        clips += c

    video = {tier: {lv: seconds * att[lv] * prices[res] for lv in LEVELS}
             for tier, prices in veo["api_per_second"].items()}
    plan = veo["flow_plan"]
    flow = {}
    for tier, per_clip in veo["flow_credits_per_clip"].items():
        credits = {lv: clips * att[lv] * per_clip for lv in LEVELS}
        months = {lv: credits[lv] / plan["monthly_credits"] for lv in LEVELS}
        flow[tier] = {**{f"credits_{lv}": credits[lv] for lv in LEVELS},
                      **{f"months_{lv}": months[lv] for lv in LEVELS},
                      **{f"usd_{lv}": math.ceil(months[lv]) * plan["monthly_usd"] for lv in LEVELS}}

    gem = pricing.get("gemini", {})
    prov = cfg.get("providers", {})
    veo_api = prov.get("video") == "veo"
    gemini_tts = prov.get("tts") == "gemini" or (prov.get("tts") == "inbox"
                                                 and cfg.get("voice", {}).get("inbox_fallback") == "gemini")
    lead_shots = sum(1 for *_, leads in stats.shots if leads)
    keyframes = {lv: (lead_shots * att[lv] * gem.get("image_per_image", 0.04) if veo_api and mode == "keyframe" else 0.0)
                 for lv in LEVELS}
    tts = {lv: (stats.lines * 5 / 60 * gem.get("tts_per_minute", 0.02) * {"low": 1, "expected": 2, "high": 3}[lv]
                if gemini_tts else 0.0) for lv in LEVELS}
    n_chars = 3
    if project_dir is not None and (project_dir / "01_story_bible.json").exists():
        n_chars = len(json.loads((project_dir / "01_story_bible.json").read_text(encoding="utf-8"))["characters"])
    identity = n_chars * 4 * gem.get("image_per_image", 0.04)     # front, 3/4 and ~2 costumes each

    cloud = gpu_cloud(pricing)
    lip_shots = sum(1 for _, _, ls, _ in stats.shots if ls)
    lip_hours = {lv: cloud["setup_hours"] + lip_shots * att[lv] * cloud["lipsync_minutes_per_shot"][lv] / 60
                 for lv in LEVELS}
    lipsync = {lv: lip_hours[lv] * cloud["hourly"] for lv in LEVELS}
    storage = {lv: seconds * att[lv] * VEO_MB_PER_S / 1024 * cloud["storage_per_gb_month"] for lv in LEVELS}

    mac = pricing["mac"]
    n = len(stats.shots)
    mac_hours = {lv: (n * mac["keyframe_candidates_per_shot"] * mac["keyframe_seconds_per_image"][lv]
                      + stats.lines * 3 * mac["tts_seconds_per_line"][lv]) / 3600 for lv in LEVELS}

    return Estimate(
        stats=stats, veo_mode=mode, veo_resolution=res, veo_seconds=seconds, veo_clips=clips, video_options=video, flow=flow,
        keyframes=keyframes, tts=tts, identity_pack=identity, ledger=_ledger_summary(project_dir),
        claude_api=claude_api_estimate(stats, pricing), claude_billing=pricing["claude"].get("billing", "api"),
        claude_logged=claude_actual(project_dir, pricing), lipsync=lipsync, lipsync_hours=lip_hours,
        storage=storage, mac_hours=mac_hours, actual_veo=actual_veo_usage(project_dir, pricing),
    )


def _ledger_summary(project_dir: Path | None) -> dict | None:
    if project_dir is None or not (project_dir / "reports" / "cost.jsonl").exists():
        return None
    ledger = SpendLedger(project_dir / "reports" / "cost.jsonl", None)
    return {"by_provider": ledger.summary(), "total": ledger.spent(), "calls": len(ledger.records())}


def preview_run(root: Path, cfg: dict, pricing: dict) -> dict:
    """What the next `run` would spend on paid generators, given what is already generated and cached."""
    from .shotpack import clips_for, inbox_dir, selected

    g = cfg.get("google", {})
    prov = cfg.get("providers", {})
    mode = g.get("veo_mode", "keyframe")
    only = cfg.get("render", {}).get("only") or []
    veo = pricing["veo"]
    res = g.get("veo_resolution", "720p")
    per_s = veo["api_per_second"][g.get("veo_tier", "fast")][res]
    takes = load_takes(root)
    image = pricing.get("gemini", {}).get("image_per_image", 0.04)
    out = {"shots": 0, "cached_shots": 0, "seconds": 0, "keyframes": 0, "lines": 0, "video_usd": 0.0,
           "keyframe_usd": 0.0, "tts_usd": 0.0, "cues": 0, "cached_cues": 0, "music_usd": 0.0}
    for path in sorted((root / "04_shots").glob("*.json")):
        shot_list = ShotList.model_validate_json(path.read_text(encoding="utf-8"))
        for shot in shot_list.shots:
            if not selected(shot, shot_list.scene_id, only):
                continue
            if prov.get("video") != "veo":
                continue
            attempt = takes.get(shot.shot_id, 1)
            if len(clips_for(root, shot.shot_id)) >= attempt:
                out["cached_shots"] += 1
                continue
            out["shots"] += 1
            out["seconds"] += veo_clip_seconds(shot.duration_s, pricing, res, mode, bool(shot.characters_in_frame))
            keyframe = root / "06_keyframes" / shot.shot_id / f"take_{attempt}.png"
            if mode == "keyframe" and shot.characters_in_frame and not keyframe.exists():
                out["keyframes"] += 1
    uses_gemini_tts = prov.get("tts") == "gemini" or (prov.get("tts") == "inbox"
                                                       and cfg.get("voice", {}).get("inbox_fallback") == "gemini")
    if uses_gemini_tts:
        for path in sorted((root / "03_scenes").glob("*.json")):
            for b in SceneScript.model_validate_json(path.read_text(encoding="utf-8")).blocks:
                if b.kind == "dialogue" and not any((inbox_dir(root, "dialogue") / f"{b.block_id}{e}").exists()
                                                    for e in (".wav", ".mp3", ".m4a", ".flac", ".aiff")):
                    out["lines"] += 1
    out["video_usd"] = out["seconds"] * per_s
    out["keyframe_usd"] = out["keyframes"] * image
    out["tts_usd"] = out["lines"] * 5 / 60 * pricing.get("gemini", {}).get("tts_per_minute", 0.02)
    if prov.get("music") == "lyria":
        from .providers.lyria import music_model

        out["cues"], out["cached_cues"] = _lyria_preview(root, cfg)
        out["music_usd"] = out["cues"] * lyria_price(pricing, music_model(g))
    out["total_usd"] = out["video_usd"] + out["keyframe_usd"] + out["tts_usd"] + out["music_usd"]
    return out


def _lyria_preview(root: Path, cfg: dict) -> tuple[int, int]:
    """(cues Lyria will generate, cues already cached or in inbox/music). Every cue is rendered whatever
    render.only says; the trailer's cue counts while trailer.mp4 is not built (its length is only known then)."""
    from .config import load_navarasa
    from .planning.music import plan_cues
    from .providers.inbox import _find_audio
    from .providers.lyria import cached_raw, characters, cue_prompt, music_model
    from .schemas import CueSheet, Outline, StoryBible

    navarasa, model, chars = load_navarasa(), music_model(cfg.get("google", {})), characters(root)
    inbox = root / "inbox" / "music"
    sheet = root / "08_music" / "cue_sheet.json"
    if sheet.exists():
        cues = CueSheet.model_validate_json(sheet.read_text(encoding="utf-8")).cues
    elif (root / "02_outline.json").exists() and (root / "01_story_bible.json").exists():   # before the music plan:
        cues = plan_cues(Outline.model_validate_json((root / "02_outline.json").read_text(encoding="utf-8")),  # same count
                         StoryBible.model_validate_json((root / "01_story_bible.json").read_text(encoding="utf-8")),
                         navarasa).cues
    else:
        cues = []
    new = cached = 0
    for cue in cues:
        if not cue.instruments:
            continue                                   # deliberate silence
        if _find_audio(inbox, cue.cue_id) or cached_raw(root, model, cue_prompt(cue, model, chars, navarasa)):
            cached += 1
        else:
            new += 1
    if not (root / "11_master" / "trailer.mp4").exists():
        if _find_audio(inbox, "trailer"):
            cached += 1
        else:
            new += 1
    return new, cached


def actual_veo_usage(project_dir: Path | None, pricing: dict) -> dict | None:
    """Clips you generated and dropped into inbox/video (every attempt counts, kept or not)."""
    if project_dir is None:
        return None
    inbox = project_dir / "inbox" / "video"
    clips = sorted(inbox.glob("*.mp4")) if inbox.exists() else []
    if not clips:
        return None
    from .post import ffmpeg

    veo = pricing["veo"]
    seconds = 0
    for clip in clips:
        seconds += billed_seconds(ffmpeg.duration(clip), veo["durations_s"], veo["extension_s"])[0]
    res = veo["resolution"]
    return {"clips": len(clips), "billed_seconds": seconds,
            "usd": {tier: seconds * prices[res] for tier, prices in veo["api_per_second"].items()},
            "flow_credits": {tier: len(clips) * c for tier, c in veo["flow_credits_per_clip"].items()}}


# ---------------------------------------------------------------------------- report

def _usd(v: float) -> str:
    return f"${v:,.0f}" if v >= 10 else f"${v:,.2f}"


def render_markdown(e: Estimate, pricing: dict) -> str:
    s, att, veo = e.stats, pricing["attempts_per_clip"], pricing["veo"]
    cloud = gpu_cloud(pricing)
    lip_shots = sum(1 for _, _, ls, _ in s.shots if ls)
    src = "from the shot plan" if s.from_plan else "from runtime only (rough)"
    out = [
        f"# Cost estimate: {s.title}",
        "",
        f"{s.minutes:.1f} min · {s.scenes} scenes · {len(s.shots)} shots · {s.lines} dialogue lines · "
        f"{lip_shots} shots need lip-sync ({src})",
        "",
        f"Attempts per clip: low {att['low']}× · expected {att['expected']}× · high {att['high']}×. "
        f"One attempt at every shot = {e.veo_clips} Veo generations, {e.veo_seconds} billed seconds "
        f"(shots round up to {veo_durations(pricing, e.veo_resolution)} s clips at {e.veo_resolution}"
        + ("; reference mode makes every shot with a lead an 8 s clip" if e.veo_mode == "reference" else "") + ").",
        "",
        f"## Video: Google Veo (the main cost), {e.veo_resolution}",
        "",
        "| Option | Low | Expected | High |",
        "|---|---|---|---|",
    ]
    names = {"standard": "Veo 3.1", "fast": "Veo 3.1 Fast", "lite": "Veo 3.1 Lite"}
    for tier, v in e.video_options.items():
        rate = veo["api_per_second"][tier][e.veo_resolution]
        out.append(f"| API: {names.get(tier, tier)} (${rate}/s) | {_usd(v['low'])} | {_usd(v['expected'])} | {_usd(v['high'])} |")
    plan = veo["flow_plan"]
    for tier, f in e.flow.items():
        per = veo["flow_credits_per_clip"][tier]
        cells = [f"{f[f'credits_{lv}']:,.0f} cr ≈ {f[f'months_{lv}']:.1f} mo ({_usd(f[f'usd_{lv}'])})" for lv in LEVELS]
        out.append(f"| {plan['name']} subscription: {names.get(tier, tier)} ({per} cr/clip) | " + " | ".join(cells) + " |")

    if e.claude_logged:
        claude_cells, claude_label = [_usd(e.claude_logged[0])] * 3, f"Claude agents ({e.claude_logged[1]} API calls logged)"
    elif e.claude_billing == "subscription":
        claude_cells, claude_label = ["$0"] * 3, "Claude agents (written in Claude Code on your subscription)"
    else:
        claude_cells, claude_label = [_usd(e.claude_api[lv]) for lv in LEVELS], "Claude agents (API, estimated)"
    out += [
        "",
        f"Subscription rows count months of one {plan['name']} plan ({plan['monthly_credits']:,} credits/month); "
        "beyond a month or two, a higher plan or the API is the realistic route.",
        "",
        "## Everything else",
        "",
        "| Item | Billed by | Low | Expected | High |",
        "|---|---|---|---|---|",
        f"| {claude_label} | Anthropic | " + " | ".join(claude_cells) + " |",
        *([f"| Claude agents, if run through the API instead | Anthropic | {_usd(e.claude_api['low'])} | "
           f"{_usd(e.claude_api['expected'])} | {_usd(e.claude_api['high'])} |"]
          if e.claude_logged or e.claude_billing == "subscription" else []),
        f"| Lip-sync on {cloud['instance']} (${cloud['hourly']}/h) | {cloud['name']} | "
        + " | ".join(f"{_usd(e.lipsync[lv])} ({e.lipsync_hours[lv]:.1f} h)" for lv in LEVELS) + " |",
        f"| Storage, 1 month | {cloud['name']} ({cloud['storage']}) | " + " | ".join(_usd(e.storage[lv]) for lv in LEVELS) + " |",
        f"| Keyframes (Veo API, {e.veo_mode} mode) | Google (Gemini image) | "
        + " | ".join(_usd(e.keyframes.get(lv, 0.0)) for lv in LEVELS) + " |",
        "| Telugu voices, if Gemini TTS | Google (Gemini TTS) | " + " | ".join(_usd(e.tts.get(lv, 0.0)) for lv in LEVELS) + " |",
        f"| Character reference images (one-time) | Google (Gemini image) | {_usd(e.identity_pack)} | "
        f"{_usd(e.identity_pack)} | {_usd(e.identity_pack)} |",
        "| Quality checks, edit, colour, mix | Your Mac + DaVinci Resolve | $0 | $0 | $0 |",
        "",
        "## Total bill by video option (expected)",
        "",
        "| Video option | Video | Everything else | Total |",
        "|---|---|---|---|",
    ]
    other = e.other_costs("expected")
    for tier, v in e.video_options.items():
        total = v["expected"] + other
        out.append(f"| API: {names.get(tier, tier)} | {_usd(v['expected'])} | {_usd(other)} | **{_usd(total)}** |")
    for tier, f in e.flow.items():
        out.append(f"| Subscription: {names.get(tier, tier)} | {_usd(f['usd_expected'])} | {_usd(other)} | "
                   f"**{_usd(f['usd_expected'] + other)}** |")
    out += [
        "",
        f"If keyframes and voices run on your Mac instead (no bill): about {e.mac_hours['low']:.1f}–"
        f"{e.mac_hours['high']:.1f} hours of machine time.",
    ]
    if e.ledger:
        by = ", ".join(f"{k} {_usd(v)}" for k, v in sorted(e.ledger["by_provider"].items()))
        out += ["", "## Actual spend (reports/cost.jsonl)", "",
                f"**{_usd(e.ledger['total'])}** over {e.ledger['calls']} paid calls: {by}"]
    if e.actual_veo:
        a = e.actual_veo
        out += [
            "",
            "## Actual so far (clips in inbox/video)",
            "",
            f"{a['clips']} clips, {a['billed_seconds']} billed seconds → "
            + ", ".join(f"{names.get(t, t)} {_usd(v)}" for t, v in a["usd"].items())
            + "; subscription credits: " + ", ".join(f"{names.get(t, t)} {c:,}" for t, c in a["flow_credits"].items()),
        ]
    out += ["", "Prices: config/pricing.yaml (verify before budgeting)."]
    return "\n".join(out) + "\n"
