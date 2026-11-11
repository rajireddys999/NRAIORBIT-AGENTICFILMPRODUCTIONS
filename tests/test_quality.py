"""Two tool sets, HD and SD (config/quality.yaml): a film's `quality` fills in the tools it does not name itself,
the queue sheet and the control room pick it per film, the cost step prices lip-sync on your own GPU, and an agent
can run on its own model when the set says so."""

import json

import pytest
import yaml
from pydantic import BaseModel

from nraiorbit import tools
from nraiorbit.agents.base import Agent
from nraiorbit.config import UnknownQuality, load_pipeline_config, quality_names, with_quality
from nraiorbit.costing import claude_actual, claude_rates, gpu_price, load_pricing, preview_run, speaking_shots
from nraiorbit.demo import OUTLINE, SCENES, SHOTS
from nraiorbit.llm import ReplayLLM
from nraiorbit.schemas import Outline, SceneScript, ShotList
from nraiorbit.service import queue


def test_a_set_fills_in_what_a_film_does_not_say(tmp_path):
    assert quality_names() == ["hd", "sd"]
    base = tools.defaults()
    hd, sd = with_quality(base, {"quality": "hd"}), with_quality(base, {"quality": " SD "})
    assert hd["providers"]["lipsync"] == "sync_so" and hd["lipsync"]["model"] == "sync-3"
    assert hd["google"]["veo_tier"] == "fast" and hd["llm"]["models"] == {} and hd["quality"] == "hd"
    assert sd["providers"]["lipsync"] == "musetalk_cloudrun" and sd["lipsync"]["runner"] == "vertex_endpoint"
    assert sd["lipsync"]["image_tag"] == "v1" and sd["lipsync"]["batch_max_shots"] == 24
    assert sd["google"]["veo_model"] == "veo-3.1-lite-generate-001" and sd["google"]["veo_tier"] == "lite"
    assert sd["llm"]["models"]["director"] == "claude-sonnet-5-5" and "screenwriter" not in sd["llm"]["models"]
    assert sd["quality"] == "sd"
    # what the film's own file says wins over the set; the set still fills in the rest
    own = {"quality": "sd", "providers": {"lipsync": "sync_so"}, "lipsync": {"model": "lipsync-2"}}
    mixed = with_quality(base, own)
    assert mixed["providers"]["lipsync"] == "sync_so" and mixed["lipsync"]["model"] == "lipsync-2"
    assert mixed["lipsync"]["runner"] == "vertex_endpoint" and mixed["google"]["veo_tier"] == "lite"
    # no quality: the defaults and the file, as before (a hand-made project runs on mocks)
    plain = with_quality(base, {"film": {"runtime_minutes": 3}})
    assert plain["providers"]["lipsync"] == "none" and "quality" not in plain
    with pytest.raises(UnknownQuality, match="hd, sd"):
        with_quality(base, {"quality": "ultra"})
    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump({"quality": "sd", "film": {"runtime_minutes": 3}}))
    cfg = load_pipeline_config(tmp_path)
    assert cfg["providers"]["lipsync"] == "musetalk_cloudrun" and cfg["film"]["runtime_minutes"] == 3


def test_the_template_is_on_hd_and_a_row_puts_a_film_on_sd():
    cfg, planned = tools.resolve(None)
    assert planned and cfg["quality"] == "hd" and cfg["providers"]["lipsync"] == "sync_so"
    assert "sync.so sync-3" in tools.line(tools.stage_tools(cfg)["render"])
    cfg, _ = tools.resolve(None, row={"quality": "sd"})
    st = tools.stage_tools(cfg)
    render = tools.line(st["render"])
    assert "MuseTalk (your Vertex AI L4)" in render and "Veo 3.1 Lite 720p" in render and "sync.so" not in render
    # the slower, sharper engine stays a film's choice away
    own = tools.resolve({"quality": "sd", "providers": {"lipsync": "latentsync_cloudrun"}})[0]
    assert "LatentSync (your Vertex AI L4)" in tools.line(tools.stage_tools(own)["render"])
    assert st["shots"][0]["name"] == "Claude Sonnet 5.5" and st["shots"][0]["model"] == "claude-sonnet-5-5 (director)"
    assert st["story"][0]["name"] == "Claude Opus 5.5" and st["outline"][0]["name"] == "Claude Opus 5.5"
    assert tools.quality_set(cfg)["label"] == "SD" and tools.quality_set(cfg)["per_minute"] == 4.0
    # a film with its own config: the row's set wins (the next tick writes it into the project)
    cfg, planned = tools.resolve({"quality": "hd", "providers": {"video": "veo"}}, row={"quality": "sd"})
    assert not planned and cfg["providers"]["lipsync"] == "musetalk_cloudrun" and cfg["quality"] == "sd"
    # a word that is not a set shows the film as its files say, named as written
    cfg, _ = tools.resolve({"quality": "ultra", "providers": {"video": "veo"}})
    assert cfg["providers"]["lipsync"] == "none" and tools.quality_set(cfg)["about"].startswith("not a set")
    assert tools.quality_set(tools.defaults()) is None
    menu = tools.quality_menu()
    assert [m["key"] for m in menu] == ["hd", "sd"] and all(m["per_minute"] > 0 and m["about"] for m in menu)


def test_the_sheets_quality_column_reaches_the_project(tmp_path):
    assert queue.STORY_COLUMNS[-1] == "quality"
    old = "slug,story_uri,budget_usd,gates,publish,state,note,updated\nrain,x,40,ask,private,new,,\n"
    assert queue.rows_of(old, queue.STORY_COLUMNS)[0]["quality"] == ""          # an older sheet still reads
    (tmp_path / "pipeline.yaml").write_text("quality: hd\nfilm: {runtime_minutes: 5}\n")
    assert queue.apply_row(tmp_path, {"quality": "SD"})
    assert yaml.safe_load((tmp_path / "pipeline.yaml").read_text())["quality"] == "sd"
    assert not queue.apply_row(tmp_path, {"quality": "ultra"})                   # not a set: left to the owner
    assert yaml.safe_load((tmp_path / "pipeline.yaml").read_text())["quality"] == "sd"
    assert not queue.apply_row(tmp_path, {"quality": ""})


class Note(BaseModel):
    text: str


def test_an_agent_runs_on_its_own_model_when_the_set_says_so():
    class Director(Agent):
        name = "director"

    class Writer(Agent):
        name = "screenwriter"

    llm = ReplayLLM({"Note": {"text": "ok"}})
    cfg = {"llm": {"models": {"director": "claude-sonnet-5-5"}}, "references": {"enabled": False}}
    assert Director(llm, cfg).ask(Note, context="", task="t").text == "ok"
    Writer(llm, cfg).ask(Note, context="", task="t")
    assert llm.models == [("director", "claude-sonnet-5-5"), ("screenwriter", None)]
    assert Director(llm, {"llm": {}}).model is None                             # no llm.models: the client's model


def _plan(root):
    (root / "03_scenes").mkdir()
    (root / "04_shots").mkdir()
    for sc in Outline.model_validate(OUTLINE).scenes:
        if sc.scene_id in SCENES:
            script = SceneScript.model_validate({"scene_id": sc.scene_id, "heading": sc.heading, "synopsis": sc.synopsis,
                                                 "rasa_primary": sc.rasa_primary, "intensity": sc.intensity,
                                                 "blocks": SCENES[sc.scene_id]})
            (root / "03_scenes" / f"{sc.scene_id}.json").write_text(script.model_dump_json(), encoding="utf-8")
        if sc.scene_id in SHOTS:
            sl = ShotList.model_validate({"scene_id": sc.scene_id, "shots": SHOTS[sc.scene_id]})
            (root / "04_shots" / f"{sc.scene_id}.json").write_text(sl.model_dump_json(), encoding="utf-8")


def test_the_cost_step_prices_gpu_lip_sync_and_claude_by_model(tmp_path):
    pricing = load_pricing()
    assert claude_rates(pricing, "claude-sonnet-5-5-20261101")["output"] == 10.0
    assert claude_rates(pricing, "claude-opus-5-5")["output"] == 20.0 and claude_rates(pricing, None)["input"] == 4.0
    assert gpu_price(pricing, "vertex_endpoint")["startup_minutes"] == 55
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "llm_usage.jsonl").write_text(json.dumps({
        "model": "claude-sonnet-5-5", "input_tokens": 1_000_000, "output_tokens": 0}) + "\n", encoding="utf-8")
    assert claude_actual(tmp_path, pricing) == (2.0, 1)                          # Sonnet's rate, not Opus's
    _plan(tmp_path)
    hd = with_quality(tools.defaults(), {"quality": "hd", "providers": {"video": "mock"}})
    sd = with_quality(tools.defaults(), {"quality": "sd", "providers": {"video": "mock"},
                                         "lipsync": {"batch_max_shots": 2}})
    paid, gpu = preview_run(tmp_path, hd, pricing), preview_run(tmp_path, sd, pricing)
    assert paid["lipsync_seconds"] == gpu["lipsync_seconds"] > 0
    assert paid["lipsync_usd"] == pytest.approx(paid["lipsync_seconds"] * 0.133, abs=0.01)
    n = len(speaking_shots(tmp_path, []))                                        # shots with a speaker in frame
    assert n >= 2
    p = gpu_price(pricing, "vertex_endpoint", "musetalk")                       # the SD set's engine
    assert p["minutes_per_clip"] < gpu_price(pricing, "vertex_endpoint")["minutes_per_clip"]   # faster than LatentSync
    batches = -(-n // 2)                                                         # batch_max_shots: 2
    assert gpu["lipsync_usd"] == pytest.approx(p["per_hour"] * (batches * p["startup_minutes"]
                                                                + n * p["minutes_per_clip"]) / 60, abs=0.01)
    assert gpu["lipsync_usd"] < paid["lipsync_usd"]
    assert preview_run(tmp_path, with_quality(tools.defaults(), {"quality": "sd", "lipsync": {"offline": True}}),
                       pricing)["lipsync_usd"] == 0
