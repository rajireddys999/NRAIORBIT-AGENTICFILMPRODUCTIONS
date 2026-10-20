"""The tool behind each control-room stage, read from a film's resolved config: what the runner will really call,
mocks named plainly, and the template's choices marked as planned until a film has its own config."""

import itertools
from types import SimpleNamespace

from nraiorbit import tools
from nraiorbit.config import deep_merge
from nraiorbit.ui.films import STAGES

MONKEY = {"film": {"runtime_minutes": 3}, "budget": {"max_usd": 30}, "policy": {"gates": "ask", "publish": "private"}}


def planned(**over):
    """The template's config (what a queued film starts from), with `over` merged on top."""
    cfg, _ = tools.resolve(None)
    return deep_merge(cfg, over)


def names(cfg, stage, shots=None):
    return [t["name"] for t in tools.stage_tools(cfg, shots=shots)[stage]]


def test_the_defaults_render_on_mocks():
    cfg = tools.defaults()
    render = tools.stage_tools(cfg)["render"]
    media = [t for t in render if t["phase"] == "render"]
    assert media and all((t["name"].endswith(": " + tools.MOCK) or t["name"] == "lips: none") and not t["paid"]
                         and t["mock"] for t in media)
    assert [t["name"] for t in media] == [f"picture: {tools.MOCK}", f"voices: {tools.MOCK}", f"music: {tools.MOCK}",
                                          "lips: none"]                          # each mock names what it stands in for
    assert names(cfg, "faces") == ["not used (video: mock)"]
    assert render[0]["phase"] == "music_plan" and render[0]["name"] == "Claude Opus 5.5 (cue sheet)"
    assert list(tools.stage_tools(cfg)) == [k for k, _ in STAGES]


def test_the_template_names_its_services():
    cfg = planned()
    st = tools.stage_tools(cfg)
    line = tools.line(st["render"])
    for want in ("Veo 3.1 Fast 720p", "inbox recordings", "ElevenLabs eleven_v4", "Lyria 3 lyria-3-pro-preview",
                 "sync.so sync-3", "speaker lock (MediaPipe)"):
        assert want in line
    assert names(cfg, "faces") == ["Gemini image gemini-2.5-flash-image"]
    assert st["story"][0]["name"] == "Claude Opus 5.5" and st["story"][0]["detail"] == "effort high"
    assert names(cfg, "scenes") == ["Claude Opus 5.5", "Claude Opus 5.5 (critic)"]
    assert st["scenes"][0]["detail"] == "effort high"                         # the configured effort, and nothing else
    assert st["scenes"][1]["detail"] == "effort medium · up to 2 rounds, pass 8"
    assert next(t for t in st["render"] if t["name"].startswith("Veo"))["detail"] == "Vertex AI · keyframe"
    assert names(cfg, "qa") == ["ffmpeg + numpy (no model)", "MediaPipe face mesh"]
    assert names(cfg, "assemble") == ["ffmpeg", "Lyria 3 lyria-3-pro-preview"]
    assert names(cfg, "publish") == ["YouTube Data API v3 (private)"]
    assert tools.render_note(cfg) is None and not tools.is_animatic(cfg)
    critic = tools.stage_tools(planned(llm={"critique_rounds": 0}))["scenes"][1]
    assert critic["name"] == "critic off" and not critic["paid"]


def test_the_faces_gate_follows_the_runners_policy():
    """produce holds the first frames for a look only under gates: ask (unset is ask); otherwise Veo draws each frame
    as it films, and the chip moves to the render stage with it."""
    cfg = planned(policy={"gates": "auto"})
    assert names(cfg, "faces") == ["skipped (gates: auto)"]
    assert "Gemini image gemini-2.5-flash-image (first frames during render)" in names(cfg, "render")
    assert tools.stage_tools(cfg)["publish"][0]["detail"] == "upload when the film check passes"
    assert names(planned(policy={"gates": None}), "faces") == ["Gemini image gemini-2.5-flash-image"]
    assert names(planned(google={"veo_mode": "reference"}), "faces") == ["none (Veo reference mode)"]
    for publish in (None, "none", "Private"):          # unset, none, or a word produce does not know: no upload
        assert names(planned(policy={"publish": publish}), "publish") == ["none"]


def test_an_inbox_without_a_fallback_falls_back_to_mock():
    cfg = planned()
    cfg["voice"].pop("inbox_fallback")
    render = names(cfg, "render")
    i = render.index("inbox recordings")
    assert render[i + 1] == f"voices: {tools.MOCK}"
    assert not tools.is_animatic(cfg)
    assert tools.render_note(cfg) == "Voices without an inbox recording fall back to mock."
    for back in ("inbox", "none"):
        cfg = planned(voice={"inbox_fallback": back})
        assert names(cfg, "render")[i + 1] == f"voices: {tools.MOCK}"
        assert tools.render_note(cfg) == "Voices without an inbox recording fall back to mock."


def test_a_paid_film_with_some_mocks_says_which():
    """Not an animatic, yet a capability still on a mock is named, so silent voices in a paid film are obvious too."""
    assert tools.render_note(planned(providers={"music": "mock"})) == "On mocks (no paid call): music."
    cfg = planned(providers={"tts": "mock", "lipsync": "mock"})
    assert tools.render_note(cfg) == "On mocks (no paid call): voices and lip-sync."
    assert {f"voices: {tools.MOCK}", f"lips: {tools.MOCK}"} <= set(names(cfg, "render"))


def test_lip_sync_offline_and_the_speaker_lock():
    cfg = planned(lipsync={"offline": True})
    sync = next(t for t in tools.stage_tools(cfg)["render"] if t["name"] == "sync.so sync-3")
    assert sync["detail"] == "cache only" and not sync["paid"]
    cfg = planned()
    cfg["lipsync"].pop("speaker_lock")
    assert "speaker lock (MediaPipe)" in names(cfg, "render")
    assert "speaker lock (MediaPipe)" not in names(planned(lipsync={"speaker_lock": False}), "render")
    lyria = tools.stage_tools(planned(music={"offline": True}))["render"]
    assert any(t["name"].startswith("Lyria 3") and t["detail"] == "cache only" and not t["paid"] for t in lyria)


def test_chants_go_to_the_crowd_provider():
    cfg = planned(voice={"crowd": {"sc012_b007": {"voices": 6}}})
    assert "Gemini TTS gemini-2.5-flash-preview-tts (chants)" in names(cfg, "render")
    assert not any("(chants)" in n for n in names(planned(), "render"))               # no crowd lines: no chip
    cfg = planned(voice={"crowd": {"x": {"voices": 3}}, "crowd_provider": "elevenlabs"})
    assert "ElevenLabs eleven_v4 (chants)" in names(cfg, "render")


def test_veo_tiers_are_the_ones_in_use():
    cfg = planned(google={"veo_tier": "ultra"})
    assert "ultra 720p" in names(cfg, "render")                                      # unknown tier: shown as given
    shots = [{"video_tier": "standard_1080p"}] * 3 + [{"video_tier": "lite_720p"}, {"video_tier": None}, {}, "x"]
    pic = next(n for n in names(planned(), "render", shots=shots) if n.startswith("Veo"))
    assert pic == "Veo 3.1 Fast 720p + 1 shot at lite_720p + 3 shots at standard_1080p"
    pic = next(n for n in names(planned(), "render") if n.startswith("Veo"))
    assert pic == "Veo 3.1 Fast 720p" and len(planned()["google"]["video_tiers"]) == 4


def test_a_film_whose_config_names_no_providers_renders_on_mocks():
    cfg, is_planned = tools.resolve(MONKEY)
    assert not is_planned and tools.is_animatic(cfg)
    note = tools.render_note(cfg)
    assert note.startswith("Renders on mocks") and "Claude planning is still billed" in note
    assert names(cfg, "publish") == ["YouTube Data API v3 (private)"]
    assert names(tools.defaults(), "publish") == ["none"]
    paid_lips = deep_merge(cfg, {"providers": {"lipsync": "sync_so"}})
    assert tools.render_note(paid_lips) == ("Picture, voices and music are mocks; lip-sync (sync.so sync-3) is paid; "
                                            "Claude planning is billed.")
    cached = deep_merge(paid_lips, {"lipsync": {"offline": True}})
    assert "lips: sync.so sync-3 (cache only). No paid render calls" in tools.render_note(cached)


def test_a_queued_row_is_planned_from_the_template():
    cfg, is_planned = tools.resolve(None, row={"gates": "auto", "publish": "none", "budget_usd": "25"})
    assert is_planned and cfg["providers"]["video"] == "veo" and cfg["providers"]["lipsync"] == "sync_so"
    assert cfg["policy"]["gates"] == "auto" and cfg["policy"]["publish"] == "none" and cfg["budget"]["max_usd"] == 25
    assert tools.template()["policy"]["gates"] == "ask"                               # the cached copy is untouched
    overrides = {"film": {"runtime_minutes": 4}, "policy": {"deliver_days": 3}}
    cfg, _ = tools.resolve({}, overrides=overrides, row={"gates": "bogus", "budget_usd": "x", "publish": "unlisted"})
    assert cfg["film"]["runtime_minutes"] == 4 and cfg["policy"]["gates"] == "ask" and cfg["budget"]["max_usd"] == 0
    assert cfg["policy"]["publish"] == "unlisted" and cfg["policy"]["deliver_days"] == 3
    cfg, _ = tools.resolve(None, overrides=overrides, row={"gates": "auto"}, use_template=False)
    assert tools.is_animatic(cfg) and cfg["policy"] == {"deliver_days": 3, "gates": "auto"}
    assert overrides["policy"] == {"deliver_days": 3}                                 # the caller's dict, as given
    st = tools.stage_tools(tools.resolve(None)[0], planned=True)
    assert all(t["planned"] for ts in st.values() for t in ts)


def test_sections_left_empty_never_break_the_page():
    """A pipeline.yaml block with every line commented out loads as null; the page must still render, and an empty
    providers block names none."""
    raw = {"providers": None, "google": None, "voice": None, "lipsync": None, "music": None, "policy": None,
           "llm": {"effort": None, "critique_rounds": "two"}}
    cfg, _ = tools.resolve(raw)
    st = tools.stage_tools(cfg, shots=[{"video_tier": "standard_1080p"}])
    assert all(t["name"] for ts in st.values() for t in ts) and tools.is_animatic(cfg)
    assert tools.render_note(cfg).startswith("Renders on mocks")
    assert st["story"][0]["detail"] == "effort high" and st["scenes"][1]["detail"].startswith("effort high · up to two")


def test_is_animatic_is_the_pipelines_rule():
    from nraiorbit.pipeline import Pipeline
    values = {"keyframe": ("mock", "none", "comfyui_flux_pulid"), "video": ("mock", "none", "veo", "shotpack"),
              "tts": ("mock", "none", "inbox", "elevenlabs"), "music": ("mock", "none", "inbox", "lyria")}
    for combo in itertools.product(*values.values()):
        c = {"providers": dict(zip(values, combo))}
        assert tools.is_animatic(c) == Pipeline._animatic(SimpleNamespace(cfg=c)), combo


def test_model_names():
    assert tools.model_name("claude-opus-5-5") == "Claude Opus 5.5"
    assert tools.model_name("claude-sonnet-4-6-20260101") == "Claude Sonnet 4.6"
    assert tools.model_name("claude-opus-5") == "Claude Opus 5"
    assert tools.model_name("claude-opus-4-5@20251101") == "Claude Opus 4.5"               # Vertex's snapshot form
    assert tools.model_name("veo-3.1-fast-generate-001") == "veo-3.1-fast-generate-001"
    assert tools.model_name(None) == ""
    assert tools.norm_model("claude-opus-5-5-20261001") == "claude-opus-5-5"


def test_phase_tools_picks_the_phase_under_way():
    st = tools.stage_tools(planned())
    assert [t["name"] for t in tools.phase_tools(st["render"], "music_plan")] == ["Claude Opus 5.5 (cue sheet)"]
    assert [t["name"] for t in tools.phase_tools(st["assemble"], "trailer")] == ["Lyria 3 lyria-3-pro-preview"]
    assert tools.phase_tools(st["render"], "render") == [t for t in st["render"] if t["phase"] == "render"]
    assert tools.phase_tools(st["qa"], "nothing") == st["qa"]                       # no match: the whole stage


def test_used_reads_the_usage_and_cost_reports():
    llm = [{"agent": "story_analyst", "schema": "StoryBible", "model": "claude-opus-5-5-20261001"},
           {"agent": "screenwriter", "schema": "Outline", "model": "claude-opus-5-5"},
           {"agent": "screenwriter", "schema": "SceneScript", "model": "claude-opus-5-5"},
           {"agent": "dialogue_critic", "schema": "Critique", "model": "claude-opus-5-5"},
           {"agent": "casting", "schema": "CastingBrief", "model": "claude-opus-5-5"},
           {"agent": "director", "schema": "ShotList", "model": "claude-sonnet-4-6"},
           {"agent": "music_director", "schema": "CueSheet", "model": "claude-opus-5-5"}]
    cost = [{"provider": "gemini_image", "item": "keyframe sc001_s001", "usd": 0.04, "model": "gemini-2.5-flash-image"},
            {"provider": "veo", "item": "sc001_s001", "usd": 1.2, "model": "veo-3.1-fast-generate-001"},
            {"provider": "veo", "item": "sc001_s002", "usd": 3.2, "model": "veo-3.1-generate-001"},
            {"provider": "veo", "item": "sc001_s003", "usd": 1.2, "model": "veo-3.1-fast-generate-001"},
            {"provider": "veo", "item": "sc001_s002", "usd": -3.2, "note": "no clip: not billed"},
            {"provider": "lyria", "item": "trailer", "usd": 0.08, "model": "lyria-3-pro-preview"},
            {"provider": "lyria", "item": "cue01", "usd": 0.08, "model": "lyria-3-pro-preview"},
            {"provider": "sync_so", "item": "sync-3 sc001_s001", "usd": 0.5},
            {"provider": "gemini_tts", "item": "sc001_b002", "usd": 0.01},
            {"provider": "gemini_tts", "item": "sc001_b004", "usd": 0.01},
            {"provider": "lyria", "item": "cue02", "usd": 0.0, "model": "lyria-3-pro-preview"}]
    u = tools.used(llm, cost, planned())
    assert u["story"] == [{"name": "Claude Opus 5.5", "model": "claude-opus-5-5-20261001", "calls": 1, "usd": None,
                           "fallback": False}]                                       # a dated snapshot is no fallback
    assert [e["calls"] for e in u["outline"]] == [1] and [e["calls"] for e in u["scenes"]] == [2]
    assert u["cast"][0]["calls"] == 1
    assert u["shots"][0]["name"] == "Claude Sonnet 4.6" and u["shots"][0]["fallback"]
    assert u["faces"][0]["model"] == "gemini-2.5-flash-image"
    assert u["assemble"] == [{"name": "lyria-3-pro-preview", "model": "lyria-3-pro-preview", "calls": 1, "usd": 0.08,
                              "fallback": False}]
    render = {e["name"]: e for e in u["render"]}
    assert render["veo-3.1-fast-generate-001"]["calls"] == 2 and render["veo-3.1-fast-generate-001"]["usd"] == 2.4
    assert render["veo-3.1-generate-001"]["calls"] == 1 and render["veo-3.1-generate-001"]["usd"] == 0.0  # refunded
    assert render["sync_so"]["calls"] == 1                                           # no model: shown by provider
    assert render["gemini_tts"]["calls"] == 2 and render["gemini_tts"]["usd"] == 0.02
    assert render["lyria-3-pro-preview"]["calls"] == 1                               # a free row is no call
    assert render["Claude Opus 5.5"]["calls"] == 1
    assert tools.used([], [], None) == {}
