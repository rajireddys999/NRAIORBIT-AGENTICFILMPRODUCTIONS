"""Which service or model does each step's work, read from a film's resolved config.

The control room shows this beside every stage (Claude Opus 5.5, Veo 3.1 Fast 720p, ElevenLabs eleven_v4, ...), and
it must say what the runner will really call: a film whose pipeline.yaml names no providers runs on mocks, and the
page has to say so plainly instead of naming the template's services. So the rules here follow the pipeline's own
(Pipeline._animatic and _provider_kwargs, produce.policy_of and _faces_needed) and its defaults. This module never
imports the pipeline: the control room only reads files, and the runner's code would bring its providers along.
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter, defaultdict
from functools import lru_cache

from .config import DEFAULT_CONFIG_DIR, REPO_ROOT, deep_merge, load_yaml
from .costing import VEO_TIER_NAMES

MOCK = "mock (no paid call)"
MOCKS = ("mock", "none")
_DATE = re.compile(r"[-@]\d{8}$")
_CLAUDE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?$")


@lru_cache(maxsize=1)
def _defaults() -> dict:
    return load_yaml(DEFAULT_CONFIG_DIR / "pipeline.yaml")


@lru_cache(maxsize=1)
def _template() -> dict:
    path = REPO_ROOT / "projects" / "_template" / "pipeline.yaml"
    return load_yaml(path) if path.exists() else {}


def defaults() -> dict:
    """config/pipeline.yaml, read once per process (callers get a copy they may change)."""
    return copy.deepcopy(_defaults())


def template() -> dict:
    """The project template's pipeline.yaml (what the runner starts every queued film from), {} when absent."""
    return copy.deepcopy(_template())


def _sec(cfg: dict, key: str) -> dict:
    """A section of the config as a mapping: one with every line commented out reads as null, and must not break
    the page."""
    value = cfg.get(key)
    return value if isinstance(value, dict) else {}


def resolve(raw, *, overrides=None, row=None, use_template=True) -> tuple[dict, bool]:
    """(config, planned). A film with its own pipeline.yaml is that file over the defaults, exactly what the runner's
    Pipeline sees. A queued film before its first tick has none yet: it is shown as the runner will make it
    (queue.make_project, then apply_row): the template, the story's overrides, then the row's cap and policy."""
    if isinstance(raw, dict) and raw:
        cfg, planned = deep_merge(defaults(), raw), False
    else:
        cfg, planned = defaults(), True
        if use_template:
            cfg = deep_merge(cfg, template())
        if isinstance(overrides, dict):
            cfg = deep_merge(cfg, overrides)
        _apply_row(cfg, row or {})
    if not isinstance(cfg.get("providers"), dict):         # an empty providers: block names none
        cfg["providers"] = defaults()["providers"]
    return cfg, planned


def _apply_row(cfg: dict, row: dict) -> None:
    """The row's gates, publish and cap as apply_row will write them, on new dicts (the overrides stay the caller's)."""
    pol = dict(_sec(cfg, "policy"))
    if row.get("gates") in ("ask", "auto"):
        pol["gates"] = row["gates"]
    if row.get("publish") in ("none", "private", "unlisted", "public"):
        pol["publish"] = row["publish"]
    cfg["policy"] = pol
    try:
        cap = float(row.get("budget_usd") or "")
    except (TypeError, ValueError):
        return
    if math.isfinite(cap) and cap > 0:                     # a sheet cannot switch the hard stop off
        cfg["budget"] = {**_sec(cfg, "budget"), "max_usd": cap}


def is_animatic(cfg: dict) -> bool:
    """Pipeline._animatic's rule, verbatim: every generator mock or none."""
    return all(cfg["providers"].get(k) in ("mock", "none") for k in ("keyframe", "video", "tts", "music"))


def norm_model(model_id) -> str:
    """A model id without its snapshot date (-YYYYMMDD, @YYYYMMDD on Vertex), so a dated served model matches the
    configured alias."""
    return _DATE.sub("", str(model_id or ""))


def model_name(model_id) -> str:
    """'claude-opus-5-5' -> 'Claude Opus 5.5', 'claude-opus-5' -> 'Claude Opus 5' (a dated snapshot too); anything
    else as given."""
    m = _CLAUDE.match(norm_model(model_id))
    if not m:
        return str(model_id or "")
    family, major, minor = m.groups()
    return f"Claude {family.capitalize()} {major}" + (f".{minor}" if minor else "")


def _tool(name, *, model="", detail="", paid=True, mock=False, phase="") -> dict:
    return {"name": str(name), "model": str(model), "detail": detail, "paid": paid, "mock": mock, "phase": phase,
            "planned": False}


def _mock(what, detail, phase="render") -> dict:
    """A capability on a mock, named for what it stands in for, so a stage with several reads at a glance."""
    return _tool(f"{what}: {MOCK}", model="mock", detail=detail, paid=False, mock=True, phase=phase)


def _claude(cfg: dict, agent: str, phase: str, extra: str = "", label: str = "") -> dict:
    """One agent's Claude call; `label` tells two agents apart where they share a stage."""
    llm = _sec(cfg, "llm")
    effort = _sec(llm, "effort").get(agent, "high")       # agents/base.py's default
    name = model_name(llm.get("model")) or "Claude"
    return _tool(f"{name} ({label})" if label else name, model=f"{llm.get('model')} ({agent})",
                 detail=f"effort {effort}{extra}", phase=phase)


def _fallback(cfg: dict) -> str:
    """What voices the lines the inbox has no recording for (_provider_kwargs: an inbox fallback is the mock)."""
    back = _sec(cfg, "voice").get("inbox_fallback", "mock")
    return "mock" if back in ("inbox", None, "") else str(back)


def _voices(cfg: dict, name, fallback: bool = False) -> list[dict]:
    """The tts provider's chips (pipeline._provider_kwargs: inbox takes recordings first, then its fallback)."""
    g, voice = _sec(cfg, "google"), _sec(cfg, "voice")
    rest = " without a recording" if fallback else ""
    if name == "elevenlabs":
        model = _sec(voice, "elevenlabs").get("model") or "eleven_v4"
        return [_tool(f"ElevenLabs {model}", model=model, detail="voices" + rest, phase="render")]
    if name == "gemini":
        model = g.get("tts_model") or "gemini-2.5-flash-preview-tts"
        return [_tool(f"Gemini TTS {model}", model=model, detail="voices" + rest, phase="render")]
    if name == "indic_parler":
        return [_tool("Indic Parler-TTS", model="indic_parler", detail="open model, run here" + rest, paid=False,
                      phase="render")]
    if name == "inbox" and not fallback:
        return [_tool("inbox recordings", model="inbox/dialogue", detail="a recorded take per line, when there is one",
                      paid=False, phase="render")] + _voices(cfg, _fallback(cfg), fallback=True)
    if name in MOCKS or name == "inbox":
        return [_mock("voices", "tone" + (" for lines without a recording" if fallback else ""))]
    return [_tool(name, model=name, detail="tts adapter" + rest, phase="render")]


def _chants(cfg: dict) -> list[dict]:
    """voice.crowd lines go to voice.crowd_provider, and only through the ElevenLabs provider (_provider_kwargs)."""
    voice, g = _sec(cfg, "voice"), _sec(cfg, "google")
    tts = _sec(cfg, "providers").get("tts")
    if not voice.get("crowd") or "elevenlabs" not in (tts, _fallback(cfg) if tts == "inbox" else None):
        return []
    crowd = voice.get("crowd_provider")
    if crowd == "gemini":
        model = g.get("tts_model") or "gemini-2.5-flash-preview-tts"
        return [_tool(f"Gemini TTS {model} (chants)", model=model, detail="voice.crowd: several voices", phase="render")]
    if crowd == "elevenlabs":
        model = _sec(voice, "elevenlabs").get("model") or "eleven_v4"
        return [_tool(f"ElevenLabs {model} (chants)", model=model, detail="voice.crowd", phase="render")]
    return []


def _music(cfg: dict, phase: str, what: str) -> list[dict]:
    name = _sec(cfg, "providers").get("music", "mock")
    if name == "lyria":
        model = _sec(cfg, "google").get("music_model") or "lyria-3-pro-preview"
        offline = bool(_sec(cfg, "music").get("offline"))
        return [_tool(f"Lyria 3 {model}", model=model, detail="cache only" if offline else what, paid=not offline,
                      phase=phase)]
    if name == "inbox":
        return [_tool("inbox tracks, else mock", model="inbox/music", detail=what, paid=False, phase=phase)]
    if name in MOCKS:
        return [_mock("music", f"{what}: placeholder", phase)]
    return [_tool(name, model=name, detail=what, phase=phase)]


def _lips(cfg: dict) -> list[dict]:
    name = _sec(cfg, "providers").get("lipsync", "none")
    ls = _sec(cfg, "lipsync")
    offline = bool(ls.get("offline"))
    lock = [_tool("speaker lock (MediaPipe)", model="post/facelock.py", detail="only the speaker's mouth changes",
                  paid=False, phase="render")] if ls.get("speaker_lock", True) else []
    if name == "sync_so":
        model = ls.get("model") or "sync-3"
        return [_tool(f"sync.so {model}", model=model, detail="cache only" if offline else "lip-sync API",
                      paid=not offline, phase="render")] + lock
    if name == "latentsync_cloudrun":
        return [_tool("LatentSync (Cloud Run L4)", model=ls.get("job") or "nraiorbit-lipsync",
                      detail="cache only" if offline else "your GPU job", paid=not offline, phase="render")] + lock
    if name == "mock":
        return [_mock("lips", "placeholder")]
    if name == "none":
        return [_tool("lips: none", model="none", detail="no lip-sync", paid=False, mock=True, phase="render")]
    return [_tool(name, model=name, detail="lip-sync adapter", phase="render")]


def _picture(cfg: dict, shots) -> list[dict]:
    name = _sec(cfg, "providers").get("video", "mock")
    g = _sec(cfg, "google")
    if name == "veo":
        tier, res = g.get("veo_tier", "fast"), g.get("veo_resolution", "720p")
        label = f"{VEO_TIER_NAMES.get(tier, tier)} {res}"
        used = Counter(sh["video_tier"] for sh in shots or () if isinstance(sh, dict)
                       and isinstance(sh.get("video_tier"), str) and sh["video_tier"])
        for t, n in sorted(used.items()):                  # only the tiers the shot lists pick, never the whole menu
            label += f" + {n} shot{'s' if n != 1 else ''} at {t}"
        where = "Vertex AI" if g.get("vertex") else "Gemini API"
        return [_tool(label, model=g.get("veo_model") or "", detail=f"{where} · {g.get('veo_mode', 'keyframe')}",
                      phase="render")]
    if name == "shotpack":
        return [_tool("hand-made clips (inbox/video)", model="shotpack", detail="clips you made", paid=False,
                      phase="render")]
    if name in MOCKS:
        return [_mock("picture", "colour cards (ffmpeg)")]
    return [_tool(name, model=name, detail="video adapter", phase="render")]


def stage_tools(cfg: dict, planned: bool = False, shots=None) -> dict[str, list[dict]]:
    """{stage key: [tool]} for every control-room stage. `shots` (the shot dicts the page loads) only adds the Veo
    tiers the shot lists actually use."""
    prov, g, llm, pol = _sec(cfg, "providers"), _sec(cfg, "google"), _sec(cfg, "llm"), _sec(cfg, "policy")
    gates = pol.get("gates") or "ask"                       # produce.policy_of: unset is ask, unset publish is none
    publish = pol.get("publish") or "none"
    rounds = llm.get("critique_rounds", 2)
    if isinstance(rounds, (int, float)) and rounds <= 0:
        critic = _tool("critic off", model="dialogue_critic", detail="llm.critique_rounds: 0", paid=False, phase="scenes")
    else:
        critic = _claude(cfg, "dialogue_critic", "scenes",
                         f" · up to {rounds} rounds, pass {llm.get('critique_pass_score', 8)}", label="critic")
    video, early = prov.get("video", "mock"), []
    if video == "veo" and g.get("veo_mode", "keyframe") == "reference":
        faces = [_tool("none (Veo reference mode)", model="veo", detail="Veo reads the cast's reference images",
                       paid=False, phase="faces")]
    elif video == "veo":
        model = g.get("image_model") or "gemini-2.5-flash-image"
        if gates == "ask":
            faces = [_tool(f"Gemini image {model}", model=model, detail="a first frame per shot with a lead",
                           phase="faces")]
        else:                                               # the faces gate holds only under ask: Veo draws each frame
            faces = [_tool(f"skipped (gates: {gates})", model=model, detail="first frames are made during render",
                           paid=False, phase="faces")]
            early = [_tool(f"Gemini image {model} (first frames during render)", model=model,
                           detail="a first frame per shot with a lead", phase="render")]
    else:
        faces = [_tool(f"not used (video: {video})", model=video, detail="first frames feed Veo only", paid=False,
                       mock=video in MOCKS, phase="faces")]
    out = {
        "story": [_claude(cfg, "story_analyst", "story_bible")],
        "outline": [_claude(cfg, "screenwriter", "outline")],
        "scenes": [_claude(cfg, "screenwriter", "scenes"), critic],
        "cast": [_claude(cfg, "casting", "cast", " · library first; a brief only for unmatched characters")],
        "shots": [_claude(cfg, "director", "shots")],
        "cost": [_tool("price check", model="costing", detail="config/pricing.yaml", paid=False, phase="cost")],
        "faces": faces,
        "render": ([_claude(cfg, "music_director", "music_plan", label="cue sheet")] + early + _picture(cfg, shots)
                   + _voices(cfg, prov.get("tts", "mock")) + _chants(cfg) + _music(cfg, "render", "score per cue")
                   + _lips(cfg)),
        "assemble": [_tool("ffmpeg", model="ffmpeg", detail="edit, mix, master, cards", paid=False, phase="assemble")]
                    + _music(cfg, "trailer", "trailer score"),
        "qa": [_tool("ffmpeg + numpy (no model)", model="ffmpeg", detail="film check", paid=False, phase="qa")]
              + ([_tool("MediaPipe face mesh", model="mediapipe", detail="lip-sync review", paid=False, phase="qa")]
                 if prov.get("lipsync", "none") != "none" else []),
        "deliver": [_tool(f"ffmpeg 720p · GCS signed links ({pol.get('deliver_days') or 7} days)", model="ffmpeg",
                          detail="delivery files", paid=False, phase="deliver")],
        "publish": [_tool(f"YouTube Data API v3 ({publish})", model="youtube", paid=False, phase="publish",
                          detail="upload after approval" if gates == "ask" else "upload when the film check passes")
                    if publish in ("private", "unlisted", "public")
                    else _tool("none", model="youtube", detail=f"policy.publish: {publish}", paid=False, phase="publish")],
    }
    if planned:
        for tools in out.values():
            for t in tools:
                t["planned"] = True
    return out


def line(tools) -> str:
    return " · ".join(t["name"] for t in tools or [])


def phase_tools(tools, phase) -> list[dict]:
    """The tools of one phase of a stage (the music plan, or the trailer), else the stage's whole list."""
    hit = [t for t in tools or [] if t.get("phase") == phase]
    return hit or list(tools or [])


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def render_note(cfg: dict) -> str | None:
    """One sentence on what renders on mocks, shared by the film page and the board; None when nothing does."""
    prov = cfg["providers"]
    lips = prov.get("lipsync", "none")
    if is_animatic(cfg):
        if lips in MOCKS or _sec(cfg, "lipsync").get("offline"):
            word = lips if lips in MOCKS else f"{_lips(cfg)[0]['name']} (cache only)"
            return (f"Renders on mocks: picture, voices, music · lips: {word}. No paid render calls; "
                    "Claude planning is still billed.")
        return f"Picture, voices and music are mocks; lip-sync ({_lips(cfg)[0]['name']}) is paid; Claude planning is billed."
    mocked = [what for what, key in (("picture", "video"), ("voices", "tts"), ("music", "music")) if prov.get(key) in MOCKS]
    if lips == "mock":
        mocked.append("lip-sync")
    notes = [f"On mocks (no paid call): {_and(mocked)}."] if mocked else []
    if prov.get("tts") == "inbox" and _fallback(cfg) in MOCKS:
        notes.append("Voices without an inbox recording fall back to mock.")
    return " ".join(notes) or None


_LLM_STAGE = {"story_analyst": "story", "casting": "cast", "director": "shots", "music_director": "render",
              "dialogue_critic": "scenes"}
_COST_RENDER = {"veo", "elevenlabs", "gemini_tts", "lyria", "sync_so", "lipsync"}


def _llm_stage(row: dict) -> str | None:
    agent, schema = row.get("agent"), row.get("schema")
    if agent == "screenwriter":
        return "outline" if schema == "Outline" else "scenes"
    if schema == "Critique":
        return "scenes"
    return _LLM_STAGE.get(agent)


def _cost_stage(row: dict) -> str | None:
    provider = row.get("provider")
    if provider == "gemini_image":
        return "faces"
    if provider == "lyria" and str(row.get("item") or "").startswith("trailer"):
        return "assemble"
    return "render" if provider in _COST_RENDER else None


def used(llm_rows, cost_rows, cfg: dict | None = None) -> dict[str, list[dict]]:
    """{stage: [{name, model, calls, usd, fallback}]} from reports/llm_usage.jsonl (the SERVED model, every row a
    call, no price there) and reports/cost.jsonl (only rows that cost something are calls; refunds are netted). A
    Claude model other than llm.model is a fallback the API served instead."""
    want = norm_model(_sec(cfg or {}, "llm").get("model"))
    acc: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for r in llm_rows or []:
        stage = _llm_stage(r) if isinstance(r, dict) else None
        if stage is None:
            continue
        served = str(r.get("model") or "")
        e = acc[stage].setdefault(("claude", served), {"name": model_name(served) or "Claude", "model": served,
                                                       "calls": 0, "usd": None,
                                                       "fallback": bool(want and served) and norm_model(served) != want})
        e["calls"] += 1
    charged: dict[tuple, tuple] = {}                        # (stage, provider, item) -> the entry its charge went to
    for r in cost_rows or []:
        stage = _cost_stage(r) if isinstance(r, dict) else None
        if stage is None:
            continue
        try:
            usd = float(r.get("usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        provider, model, item = r.get("provider"), r.get("model"), r.get("item")
        if model:
            key = charged[(stage, provider, item)] = (provider, model)
        else:                   # Veo's refund ("no clip: not billed") names no model: it nets into the charge it undoes
            key = charged.get((stage, provider, item)) or next((k for k in acc[stage] if k[0] == provider),
                                                               (provider, None))
        e = acc[stage].setdefault(key, {"name": str(model or provider), "model": str(model or provider), "calls": 0,
                                        "usd": 0.0, "fallback": False})
        e["usd"] = round(e["usd"] + usd, 4)
        if usd > 0:
            e["calls"] += 1
    return {stage: list(entries.values()) for stage, entries in acc.items()}
