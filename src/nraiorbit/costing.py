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


def approx_tokens(text: str) -> int:
    """~4 bytes per token: right for English JSON; Telugu (3 bytes/char) lands near 0.75 token/char."""
    return max(1, len(text.encode("utf-8")) // 4)


@dataclass
class PlanStats:
    title: str
    minutes: float
    scenes: int
    shots: list[tuple[str, float, bool]]   # (shot_id, duration_s, needs_lipsync)
    lines: int
    from_plan: bool
    artifact_tokens: dict[str, int] = field(default_factory=dict)


def plan_stats(project_dir: Path | None, runtime_minutes: float | None = None) -> PlanStats:
    if project_dir is not None and (project_dir / "04_shots").exists() and any((project_dir / "04_shots").glob("*.json")):
        return _stats_from_plan(project_dir)
    minutes = runtime_minutes or 105
    n = int(round(minutes * 60 / AVG_SHOT_S))
    lines = int(round(n * LINES_PER_SHOT))
    shots = [(f"shot{i:04d}", AVG_SHOT_S, i < lines) for i in range(n)]
    return PlanStats(f"{minutes:g}-minute film (no shot plan yet)", minutes, int(round(minutes * 60 / SCENE_S)),
                     shots, lines, from_plan=False)


def _stats_from_plan(root: Path) -> PlanStats:
    tokens: dict[str, int] = {}
    bible_path = root / "01_story_bible.json"
    title = json.loads(bible_path.read_text(encoding="utf-8"))["title_en"] if bible_path.exists() else root.name
    for name in ("01_story_bible.json", "02_outline.json"):
        if (root / name).exists():
            tokens[name] = approx_tokens((root / name).read_text(encoding="utf-8"))
    shots: list[tuple[str, float, bool]] = []
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
            shots.append((shot.shot_id, shot.duration_s, needs_lipsync))
    minutes = sum(d for _, d, _ in shots) / 60
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

    def other_costs(self, level: str) -> float:
        claude = self.claude_logged[0] if self.claude_logged else (
            0.0 if self.claude_billing == "subscription" else self.claude_api[level])
        return claude + self.lipsync[level] + self.storage[level]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stats"] = {k: v for k, v in d["stats"].items() if k != "shots"} | {"shot_count": len(self.stats.shots)}
        return d


def estimate(project_dir: Path | None = None, runtime_minutes: float | None = None,
             pricing: dict | None = None) -> Estimate:
    pricing = pricing or load_pricing(project_dir)
    stats = plan_stats(project_dir, runtime_minutes)
    att = pricing["attempts_per_clip"]
    veo = pricing["veo"]
    res = veo["resolution"]

    seconds = clips = 0
    for _, dur, _ in stats.shots:
        s, c = billed_seconds(dur, veo["durations_s"], veo["extension_s"])
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

    aws = pricing["aws"]
    lip_shots = sum(1 for _, _, ls in stats.shots if ls)
    lip_hours = {lv: aws["setup_hours"] + lip_shots * att[lv] * aws["lipsync_minutes_per_shot"][lv] / 60
                 for lv in LEVELS}
    lipsync = {lv: lip_hours[lv] * aws["hourly"] for lv in LEVELS}
    storage = {lv: seconds * att[lv] * VEO_MB_PER_S / 1024 * aws["s3_per_gb_month"] for lv in LEVELS}

    mac = pricing["mac"]
    n = len(stats.shots)
    mac_hours = {lv: (n * mac["keyframe_candidates_per_shot"] * mac["keyframe_seconds_per_image"][lv]
                      + stats.lines * 3 * mac["tts_seconds_per_line"][lv]) / 3600 for lv in LEVELS}

    return Estimate(
        stats=stats, veo_seconds=seconds, veo_clips=clips, video_options=video, flow=flow,
        claude_api=claude_api_estimate(stats, pricing), claude_billing=pricing["claude"].get("billing", "api"),
        claude_logged=claude_actual(project_dir, pricing), lipsync=lipsync, lipsync_hours=lip_hours,
        storage=storage, mac_hours=mac_hours, actual_veo=actual_veo_usage(project_dir, pricing),
    )


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
    lip_shots = sum(1 for _, _, ls in s.shots if ls)
    src = "from the shot plan" if s.from_plan else "from runtime only (rough)"
    out = [
        f"# Cost estimate: {s.title}",
        "",
        f"{s.minutes:.1f} min · {s.scenes} scenes · {len(s.shots)} shots · {s.lines} dialogue lines · "
        f"{lip_shots} shots need lip-sync ({src})",
        "",
        f"Attempts per clip: low {att['low']}× · expected {att['expected']}× · high {att['high']}×. "
        f"One attempt at every shot = {e.veo_clips} Veo generations, {e.veo_seconds} billed seconds "
        f"(shots round up to {veo['durations_s']} s clips).",
        "",
        f"## Video: Google Veo (the main cost), {veo['resolution']}",
        "",
        "| Option | Low | Expected | High |",
        "|---|---|---|---|",
    ]
    names = {"standard": "Veo 3.1", "fast": "Veo 3.1 Fast", "lite": "Veo 3.1 Lite"}
    for tier, v in e.video_options.items():
        rate = veo["api_per_second"][tier][veo["resolution"]]
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
        f"| Lip-sync on {pricing['aws']['instance']} (${pricing['aws']['hourly']}/h) | AWS | "
        + " | ".join(f"{_usd(e.lipsync[lv])} ({e.lipsync_hours[lv]:.1f} h)" for lv in LEVELS) + " |",
        "| Storage, 1 month | AWS S3 | " + " | ".join(_usd(e.storage[lv]) for lv in LEVELS) + " |",
        "| Keyframes, Telugu voices, checks, edit | Your Mac + DaVinci Resolve | $0 | $0 | $0 |",
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
        f"Mac time (no bill): about {e.mac_hours['low']:.1f}–{e.mac_hours['high']:.1f} hours for keyframes and voices.",
    ]
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
