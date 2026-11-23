"""A short (the control room's New Shorts tab): the same queue, runner and HD or SD tool sets as a film, with a short's
own settings on top: its own structure template told in seconds, upright 9:16 shots, no trailer, reels or title
cards, one upload as a YouTube Short. A film (no film.format) is planned, shot and published exactly as before."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nraiorbit.agents import Director, Screenwriter, StoryAnalyst
from nraiorbit.agents.production import VERTICAL_FRAMING
from nraiorbit.config import REPO_ROOT, load_navarasa, load_pipeline_config
from nraiorbit.demo import OUTLINE, SCENES, SHOTS, STORY_BIBLE
from nraiorbit.pipeline import Pipeline, ProjectPaths
from nraiorbit.planning.runtime import (
    SHORT_MAX_S, SHORT_TEMPLATE, TEMPLATES, describe_template, rebalance_outline, template_blocks, validate_outline,
)
from nraiorbit.post import ffmpeg
from nraiorbit.publish import youtube
from nraiorbit.schemas import CastLock, Outline, SceneScript, ShotList, StoryBible
from nraiorbit.service import produce, queue
from nraiorbit.shotpack import veo_prompt


@pytest.fixture
def bible():
    return StoryBible.model_validate(STORY_BIBLE)


@pytest.fixture
def outline():
    return Outline.model_validate(OUTLINE)


class Recorder:
    """An LLM that records each task it is given and answers with the fixture for the schema asked for."""

    def __init__(self, **answers):
        self.tasks, self.answers = [], answers

    def generate(self, *, schema, system, context, task, effort, agent, model=None):
        self.tasks.append(task)
        return self.answers[schema.__name__]


def test_a_short_has_its_own_template_told_in_seconds_and_no_landmarks(outline):
    assert "short" not in TEMPLATES and list(SHORT_TEMPLATE) == ["short"] and SHORT_MAX_S == 180   # `auto` never picks it
    blocks = template_blocks("short", 0.5)
    assert [b.name for b in blocks] == ["hook", "turn", "payoff"] and blocks[0].start_s == 0 and blocks[-1].end_s == 30
    said = describe_template("short", 0.5)
    assert said.startswith("Structure template 'short' for 30 seconds:") and "- hook: 0-6 s" in said and "min" not in said
    assert describe_template("thriller", 3).startswith("Structure template 'thriller' for 3 minutes:")   # as it was
    scenes = [s.model_copy(update={"set_piece": "none"}) for s in outline.scenes[:2]]
    two = rebalance_outline(Outline(scenes=scenes), 0.5, step=2)
    assert sum(s.target_seconds for s in two.scenes) == 30
    assert validate_outline(two, 0.5, 0.1, template="short") == []
    assert any("interval_bang" in issue for issue in validate_outline(two, 0.5, 0.1))   # a film is checked as before


def test_the_writers_are_told_it_is_a_short_and_a_film_is_asked_for_as_before(bible, outline):
    llm = Recorder(StoryBible=bible, Outline=outline)
    StoryAnalyst(llm, {}).run("an idea", 0.5, "short", "the guide", short_seconds=30)
    StoryAnalyst(llm, {}).run("a plot", 10, "auto", "the guide")
    short, film = llm.tasks
    assert "30-second vertical Telugu short for YouTube Shorts" in short and "Use structure_template 'short'." in short
    assert "hook in the first three seconds" in short and "feature film" not in short
    assert film.startswith("Build the Story Bible for a 10-minute Telugu feature film.") and "short" not in film.lower()
    Screenwriter(llm, {}).outline(bible, 0.5, "the guide", short_seconds=30)
    Screenwriter(llm, {}).outline(bible, 10, "the guide")
    short, film = llm.tasks[2:]
    assert "target_seconds sum to 30 seconds" in short and "No interval" in short
    assert film.startswith("Write the scene outline for the whole film. Scene target_seconds must sum to 600 seconds.")


def test_the_director_frames_a_short_for_a_phone_screen(bible, outline):
    sc = outline.scenes[0]
    script = SceneScript.model_validate({"scene_id": sc.scene_id, "heading": sc.heading, "synopsis": sc.synopsis,
                                         "rasa_primary": sc.rasa_primary, "intensity": sc.intensity,
                                         "blocks": SCENES[sc.scene_id]})
    shots = ShotList.model_validate({"scene_id": sc.scene_id, "shots": SHOTS[sc.scene_id]})
    llm = Recorder(ShotList=shots)
    director = Director(llm, {})
    director.shots(bible, CastLock(characters={}), sc, script, [], {}, framing=VERTICAL_FRAMING)
    director.shots(bible, CastLock(characters={}), sc, script, [], {})
    assert "vertical short for a phone screen (9:16, upright)" in llm.tasks[0]
    assert "9:16" not in llm.tasks[1] and "phone screen" not in llm.tasks[1]
    shot = shots.shots[0].model_copy(update={"characters_in_frame": [], "costume_ids": []})
    wide = veo_prompt(shot, sc, bible, CastLock(characters={}), load_navarasa(), None)
    tall = veo_prompt(shot, sc, bible, CastLock(characters={}), load_navarasa(), None, vertical=True)
    assert wide.endswith("natural skin texture, 24fps film look, 16:9.")
    assert tall.endswith("24fps film look, 9:16 vertical, upright for a phone screen.") and "16:9" not in tall


def _planner(tmp_path, film: dict, answer: StoryBible):
    """Pipeline._story_bible over a bare project: what the story analyst is asked, and what is saved."""
    paths = ProjectPaths(tmp_path)
    paths.story_input.parent.mkdir(parents=True, exist_ok=True)
    paths.story_input.write_text("a little girl gives her umbrella to a wet puppy", encoding="utf-8")
    asked = []

    def run(plot, runtime, template, guide, **kw):
        asked.append((template, guide, kw))
        return answer
    p = SimpleNamespace(p=paths, cfg={"film": film}, _film=film, log=lambda *_: None, gate=lambda name: None,
                        story_analyst=SimpleNamespace(run=run), _save=Pipeline._save)
    Pipeline._story_bible(p)
    return asked[0], StoryBible.model_validate_json(paths.bible.read_text(encoding="utf-8"))


def test_a_short_is_planned_from_the_short_template_whatever_the_analyst_names(tmp_path, bible):
    assert bible.structure_template != "short"
    (template, guide, kw), saved = _planner(tmp_path / "short", {"format": "short", "runtime_minutes": 0.5,
                                                               "structure_template": "auto"}, bible)
    assert template == "short" and kw == {"short_seconds": 30}
    assert guide.startswith("Structure template 'short' for 30 seconds:") and "telugu_commercial" not in guide
    assert saved.structure_template == "short"
    (template, guide, kw), saved = _planner(tmp_path / "film", {"runtime_minutes": 10, "structure_template": "auto"}, bible)
    assert template == "auto" and kw == {} and all(name in guide for name in TEMPLATES) and "'short'" not in guide
    assert saved.structure_template == bible.structure_template


def test_a_short_has_no_trailer(tmp_path):
    paths, logs = ProjectPaths(tmp_path), []
    Pipeline._trailer(SimpleNamespace(p=paths, log=logs.append, cfg={"film": {"format": "short"}}))
    assert not (paths.master / "trailer.mp4").exists() and logs == ["  trailer: none for a short"]


def test_a_shorts_settings_ride_on_the_same_tool_sets_and_a_film_is_no_short(tmp_path):
    """What the New Shorts tab writes beside the idea, through the runner's own project maker and config: upright,
    no trailer gate, no reels, no cards, on either tool set; the same template with a film's overrides is a film."""
    from nraiorbit.post import titlecards
    from nraiorbit.ui.app import short_overrides
    template = REPO_ROOT / "projects" / "_template"
    for tool_set, lips in (("hd", "sync_so"), ("sd", "musetalk_cloudrun")):
        row = {"slug": f"umbrella-{tool_set}", "budget_usd": "12", "gates": "ask", "publish": "private",
               "quality": tool_set}                              # the row is a film's row: the overrides make the short
        root = queue.make_project(tmp_path, template, row, "an idea", overrides=short_overrides(46, tool_set))
        cfg = load_pipeline_config(root)
        assert cfg["quality"] == tool_set and cfg["providers"]["lipsync"] == lips and cfg["budget"]["max_usd"] == 12
        assert cfg["film"]["format"] == "short" and cfg["film"]["resolution"] == [1080, 1920]
        assert round(cfg["film"]["runtime_minutes"] * 60) == 46 and cfg["film"]["structure_template"] == "short"
        assert cfg["google"]["veo_aspect_ratio"] == "9:16" and cfg["reels"]["enabled"] is False and cfg["cards"] == []
        assert cfg["review_gates"]["trailer"]["enabled"] is False and not titlecards.asks_for_words(cfg)
        assert cfg["review_gates"]["story"] == {"enabled": True, "auto_approve": False}     # every other gate still waits
        assert produce.is_short(cfg)
    row = {"slug": "a-film", "budget_usd": "150", "gates": "ask", "publish": "private", "quality": "hd"}
    film = load_pipeline_config(queue.make_project(tmp_path, template, row, "a story", overrides={
        "film": {"runtime_minutes": 10}, "quality": "hd"}))
    assert not produce.is_short(film) and film["film"]["format"] == "film" and film["film"]["resolution"] == [1920, 1080]
    assert film["google"]["veo_aspect_ratio"] == "16:9" and film["reels"]["enabled"] is True and film["cards"] == "auto"
    assert titlecards.asks_for_words(film)


def test_the_queue_sheet_is_the_films_own_with_no_new_column():
    """A short is a short by the settings written beside its idea, never by the sheet: the runner of any version
    reads and rewrites the same columns."""
    assert queue.STORY_COLUMNS == ["slug", "story_uri", "budget_usd", "gates", "publish", "state", "note", "updated",
                                   "quality"]


def test_a_shorts_youtube_metadata_marks_it_a_short(bible):
    meta = youtube.build_short_metadata(bible, privacy="unlisted")
    title = meta["snippet"]["title"]
    assert title == f"{bible.title_te} | {bible.title_en} #Shorts" and len(title) <= 100
    assert "#Shorts" in meta["snippet"]["description"] and "0:00" not in meta["snippet"]["description"]   # no chapters
    assert meta["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}
    long_name = bible.model_copy(update={"title_en": "A" * 200})
    assert youtube.build_short_metadata(long_name)["snippet"]["title"].endswith(" #Shorts")
    assert len(youtube.build_short_metadata(long_name)["snippet"]["title"]) == 100


def test_a_short_goes_up_once_as_a_short_with_no_trailer(tmp_path, monkeypatch, bible, outline):
    root = tmp_path / "short"
    (root / "11_master").mkdir(parents=True)
    (root / "01_story_bible.json").write_text(bible.model_dump_json(), encoding="utf-8")
    (root / "02_outline.json").write_text(outline.model_dump_json(), encoding="utf-8")
    (root / "11_master" / "movie.mp4").write_bytes(b"the short")
    (root / "11_master" / "trailer.mp4").write_bytes(b"left by a mistake: a short never uploads one")
    (root / "11_master" / "subs_te.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nఅ\n", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS", str(tmp_path / "missing.json"))
    monkeypatch.setattr(youtube, "token_available", lambda token: True)
    monkeypatch.setattr(youtube, "credentials", lambda secrets, token, interactive=True: "creds")
    sent = []
    monkeypatch.setattr(youtube, "upload", lambda video, meta, creds, captions=None, thumbnail=None: sent.append(
        (video.name, meta["snippet"]["title"], sorted(captions or {}), thumbnail)) or f"id-{len(sent)}")
    out = produce.publish_youtube(root, "private", short=True)
    assert sent == [("movie.mp4", f"{bible.title_te} | {bible.title_en} #Shorts", ["te"], None)]
    assert out["film"]["id"] == "id-1" and out["film"]["short"] is True and "trailer" not in out
    assert json.loads((root / "reports" / "youtube.json").read_text())["film"]["privacy"] == "private"
    assert produce.publish_youtube(root, "private", short=True) == out and len(sent) == 1      # never twice


def test_the_cost_step_words_and_the_gates_know_a_short():
    assert produce.is_short({"film": {"format": "short"}})
    assert not produce.is_short({"film": {"runtime_minutes": 10}}) and not produce.is_short({})
    from nraiorbit.ui import films
    assert films.stages_for("film") is films.STAGES
    short = films.stages_for("short")
    assert "cards" not in dict(short) and dict(short)["assemble"] == "The short" and len(short) == len(films.STAGES) - 1
    assert films.film_format({"film": {"format": "short"}}) == "short" and films.film_format({}) == "film"
    queued = {"film": {"format": "short", "runtime_minutes": 0.5}}                  # no project yet: its overrides say
    assert films.film_format({}, queued) == "short" and films.short_seconds({}, queued, "short") == 30
    assert films.film_format({"film": {"runtime_minutes": 10}}, None) == "film"
    assert films.film_format({"film": {"format": "film"}}, queued) == "film"        # the project's own file says
    assert films.short_seconds({"film": {"runtime_minutes": 10}}, None, "film") is None
    assert "as a YouTube Short" in films.gate_help("publish", short=True)["why"]
    assert films.gate_help("publish", short=True)["step"] == "Step 12 · YouTube"          # numbered as its page is
    assert films.gate_help("final_cut", short=True)["step"] == "Step 9 · The short"
    assert films.gate_help("publish")["step"] == "Step 13 · YouTube" and films.step_of("cards") == (11, "Title & end cards")
    assert films.gate_help("publish") == films.gate_help("publish", short=False)
    assert "the film and the trailer" in films.gate_help("publish")["why"]


def test_the_cost_estimate_counts_no_trailer_music_for_a_short(tmp_path):
    """The estimate the owner approves counts one music track for the trailer until the trailer is cut. A short never
    has one, so none is priced; a film's count is as it was."""
    from nraiorbit.costing import _lyria_preview
    film = {"google": {}, "film": {"runtime_minutes": 10}}
    assert _lyria_preview(tmp_path, film) == (1, 0)
    assert _lyria_preview(tmp_path, {"google": {}, "film": {"format": "short", "runtime_minutes": 0.5}}) == (0, 0)


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
def test_the_small_delivery_copy_keeps_an_upright_short_upright(tmp_path):
    from nraiorbit.release.deliver import encode_720p

    def size(path: Path) -> str:
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, check=True).stdout.strip()
    for name, shape, small in (("film", "1920x1080", "1280,720"), ("short", "1080x1920", "720,1280")):
        master = tmp_path / f"{name}.mp4"
        ffmpeg.run(["-f", "lavfi", "-i", f"testsrc2=s={shape}:r=24:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(master)])
        assert size(encode_720p(master, tmp_path / f"{name}_small.mp4")) == small
