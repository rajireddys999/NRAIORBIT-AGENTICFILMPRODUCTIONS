"""The queue runner's state machine, the reference learner against a fake Gemini, and the agents' reference block."""

import json
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit.agents.base import Agent, reference_examples
from nraiorbit.service import learn, queue

pytest.importorskip("google.genai")


# ------------------------------------------------------------------------------------------------------- queue

def _queues(tmp_path):
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "rain", "story_uri": str(tmp_path / "rain.md"), "budget_usd": "40", "gates": "ask",
                          "publish": "private", "state": "new", "note": "", "updated": ""}], queue.STORY_COLUMNS)
    queue.save(str(rf), [{"url": "https://www.youtube.com/watch?v=abc123xyz", "title": "Ref", "learn": "score",
                          "state": "new", "profile_uri": "", "note": "", "updated": ""}], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    return str(st), str(rf)


def test_tick_makes_the_project_runs_produce_and_moves_states(tmp_path):
    st, rf = _queues(tmp_path)
    projects = tmp_path / "projects"
    template = projects / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\npolicy: {gates: auto}\n", encoding="utf-8")
    seen = []

    def produce_fn(root, bucket):
        seen.append(root.name)
        cfg = (root / "pipeline.yaml").read_text()
        assert "max_usd: 40.0" in cfg and "gates: ask" in cfg and "publish: private" in cfg
        assert (root / "input" / "story.md").read_text() == "A ferryman and a flood."
        return {"state": "awaiting:story", "steps": {}}

    out = queue.tick(st, rf, projects, bucket=None, produce_fn=produce_fn,
                     learn_fn=lambda url, title, what, minutes: tmp_path / "profile.json", template=template,
                     log=lambda *_: None)
    assert out == {"reference": {"url": "https://www.youtube.com/watch?v=abc123xyz", "state": "done"},
                   "story": {"slug": "rain", "state": "awaiting:story"}}
    rows = queue.load(st, queue.STORY_COLUMNS)
    assert rows[0]["state"] == "awaiting:story" and rows[0]["updated"]
    assert queue.load(rf, queue.REFERENCE_COLUMNS)[0]["profile_uri"].endswith("profile.json")
    # nothing is ready now: the owner must approve
    assert queue.tick(st, rf, projects, bucket=None, produce_fn=produce_fn, learn_fn=None, log=lambda *_: None) == \
        {"reference": None, "story": None}
    # the owner approves in the sheet: the pending gate is approved on disk and produce runs again
    (projects / "rain" / "reviews").mkdir()
    (projects / "rain" / "reviews" / "story.pending").write_text("x")
    rows[0]["state"] = "approved"
    queue.save(st, rows, queue.STORY_COLUMNS)
    queue.tick(st, rf, projects, bucket=None, produce_fn=lambda r, b: {"state": "done", "steps": {"publish": "none"}},
               learn_fn=None, log=lambda *_: None)
    assert (projects / "rain" / "reviews" / "story.approved").exists()
    assert queue.load(st, queue.STORY_COLUMNS)[0]["state"] == "done" and seen == ["rain"]


def test_story_overrides_row_sync_reference_minutes_and_library_sync(tmp_path, monkeypatch):
    st, rf = _queues(tmp_path)
    (tmp_path / "rain.overrides.yaml").write_text("film: {runtime_minutes: 5}\nvoice: {cast: {hero: v1}}\n")
    refs = queue.load(rf, queue.REFERENCE_COLUMNS)
    refs[0]["minutes"] = "12"
    queue.save(rf, refs, queue.REFERENCE_COLUMNS)
    projects = tmp_path / "projects"
    (projects / "_template").mkdir(parents=True)
    (projects / "_template" / "pipeline.yaml").write_text("film: {runtime_minutes: 10}\nbudget: {max_usd: 0}\n")
    seen, calls = {}, []
    from nraiorbit.ops import backup
    monkeypatch.setattr(backup, "sync_library", lambda bucket, direction, log=print: calls.append((bucket, direction)))
    monkeypatch.setattr(backup, "restore_report",
                        lambda root, bucket, log=print: calls.append(("restore", root.name)) or (0, []))
    monkeypatch.setattr(backup, "backup",
                        lambda root, bucket, log=print, mirror_reviews=False: calls.append(("backup", root.name)))

    def produce_fn(root, bucket):
        seen["cfg"] = yaml.safe_load((root / "pipeline.yaml").read_text())
        return {"state": "awaiting:story", "steps": {}}

    def learn_fn(url, title, what, minutes):
        seen["minutes"] = minutes
        return tmp_path / "p.json"
    queue.tick(st, rf, projects, bucket="b", produce_fn=produce_fn, learn_fn=learn_fn, log=lambda *_: None)
    cfg = seen["cfg"]
    assert cfg["film"]["runtime_minutes"] == 5 and cfg["voice"]["cast"] == {"hero": "v1"}     # the overrides
    assert cfg["budget"]["max_usd"] == 40.0 and cfg["policy"] == {"gates": "ask", "publish": "private"}
    assert seen["minutes"] == "12"
    assert calls[0] == ("b", "restore") and calls[-1] == ("b", "backup")       # the library round trip
    # the owner raises the cap and changes publish in the sheet: the next tick writes both into the project
    rows = queue.load(st, queue.STORY_COLUMNS)
    rows[0].update(state="approved", budget_usd="60", publish="unlisted")
    queue.save(st, rows, queue.STORY_COLUMNS)
    queue.tick(st, rf, projects, bucket=None, produce_fn=produce_fn, learn_fn=None, log=lambda *_: None)
    assert seen["cfg"]["budget"]["max_usd"] == 60.0 and seen["cfg"]["policy"]["publish"] == "unlisted"
    assert seen["cfg"]["film"]["runtime_minutes"] == 5                      # overrides survive
    assert queue.overrides_uri("gs://b/stories/rain.md") == "gs://b/stories/rain.overrides.yaml"
    assert queue.overrides_uri("rain") == "rain.overrides.yaml"


def test_the_queued_story_replaces_the_template_placeholder(tmp_path):
    projects = tmp_path / "projects"
    (projects / "_template" / "input").mkdir(parents=True)
    (projects / "_template" / "pipeline.yaml").write_text("film: {runtime_minutes: 10}\n")
    (projects / "_template" / "input" / "story.md").write_text("# Your story goes here\n")
    row = {"slug": "rain", "budget_usd": "40", "gates": "", "publish": ""}
    root = queue.make_project(projects, projects / "_template", row, "A ferryman and a flood.")
    assert (root / "input" / "story.md").read_text() == "A ferryman and a flood."
    # a second call (the project exists) keeps whatever is there now
    (root / "input" / "story.md").write_text("edited by hand")
    queue.make_project(projects, projects / "_template", row, "A ferryman and a flood.")
    assert (root / "input" / "story.md").read_text() == "edited by hand"
    # a nan or inf cap in the sheet never reaches the project
    queue.apply_row(root, {"budget_usd": "inf"})
    assert yaml.safe_load((root / "pipeline.yaml").read_text())["budget"]["max_usd"] == 40.0


def test_tick_records_failures_as_blocked(tmp_path):
    st, rf = _queues(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("credits are out")
    out = queue.tick(st, rf, tmp_path / "projects", bucket=None, produce_fn=boom, learn_fn=boom, log=lambda *_: None)
    assert out["reference"]["state"] == "blocked:RuntimeError" and out["story"]["state"] == "blocked:RuntimeError"
    assert queue.load(st, queue.STORY_COLUMNS)[0]["note"] == "credits are out"
    assert queue.ready_story(queue.load(st, queue.STORY_COLUMNS)) is None        # blocked rows wait for the owner


# ------------------------------------------------------------------------------------------------------- learn

PROFILE = {
    "title": "Example Film", "language": "Telugu", "kind": "film", "summary": "A village hero film. Strong interval.",
    "score": {"cue_anatomy": "slow build, hit on the cut", "instruments_by_rasa": {"veera": "nadaswaram, dhol"},
              "motifs": "one hero theme", "tempo_and_percussion": "fast dhol in fights"},
    "emotion": {"beat_pacing": "long holds before violence", "rasa_curve": ["shanta", "veera", "karuna", "raudra"],
                "goosebump_moments": ["the interval reveal"]},
    "action": {"staging": "hero centred, villains in arcs", "cutting": "short, impact frames", "sound": "silence before hits"},
    "characters": {"hero": "quiet, protective", "heroine": "teacher, brave", "villain": "landlord",
                   "supporting": "ferryman, mother", "introductions": "hero from behind, then a turn"},
    "trailer": {"structure": "three movements, title at 70%", "line_choice": "threat, promise, taunt",
                "music_and_cuts": "drops before the title"},
    "lessons": ["hold the beat before the hit", "introduce the villain with a kindness first"],
    "cautions": ["do not reuse the famous catchphrase"],
}


class FakeGemini:
    def __init__(self):
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=json.dumps(PROFILE),
                               usage_metadata=SimpleNamespace(prompt_token_count=12000, candidates_token_count=900))


def test_learn_writes_a_profile_and_manifest_from_a_public_link(tmp_path):
    fake = FakeGemini()
    out = learn.learn("https://www.youtube.com/watch?v=abc123xyz", title="Example Film", what="score",
                      cfg={"google": {"analysis_model": "gemini-2.5-pro"}}, client=fake, out_dir=tmp_path / "refs",
                      start_s=60, end_s=600)
    assert out == tmp_path / "refs" / "example_film_abc123xyz" / "profile.json"
    prof = json.loads(out.read_text(encoding="utf-8"))
    assert prof["lessons"][0] == "hold the beat before the hit"
    call = fake.calls[0]
    assert call["model"] == "gemini-2.5-pro"
    video = call["contents"][0]
    assert video.file_data.file_uri == "https://www.youtube.com/watch?v=abc123xyz"     # watched by link, never downloaded
    assert video.video_metadata.fps == learn.SAMPLE_FPS and video.video_metadata.start_offset == "60s"
    assert "do not transcribe dialogue" in call["contents"][1]
    manifest = (out.parent / "manifest.yaml").read_text(encoding="utf-8")
    assert "source_url: https://www.youtube.com/watch?v=abc123xyz" in manifest and "input: 12000" in manifest
    # the newest profiles feed the agents
    profiles = learn.load_profiles(tmp_path / "refs")
    assert len(profiles) == 1
    text = learn.examples_text(profiles, parts=("summary", "lessons", "score"))
    assert "nadaswaram" in text and "catchphrase" not in text        # cautions are not pushed as examples


def test_agents_get_the_reference_block_in_their_context(tmp_path):
    (tmp_path / "refs" / "x").mkdir(parents=True)
    (tmp_path / "refs" / "x" / "profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    cfg = {"references": {"folder": str(tmp_path / "refs")}, "llm": {}}
    block = reference_examples("music_director", cfg)
    assert block.startswith("<references>") and "cue_anatomy" in block and "staging" not in block
    assert reference_examples("music_director", {"references": {"enabled": False}}) == ""
    assert reference_examples("music_director", {"references": {"folder": str(tmp_path / "none")}}) == ""

    class Capture:
        def generate(self, *, schema, system, context, task, effort, agent):
            self.context = context
            return schema()

    class Pinged(Agent):
        name = "music_director"
    llm = Capture()
    agent = Pinged(llm, cfg)

    from pydantic import BaseModel

    class Empty(BaseModel):
        pass
    agent.ask(Empty, context="<story_bible>\n{}\n</story_bible>", task="score it")
    assert llm.context.startswith("<story_bible>") and "<references>" in llm.context
