"""Nobody under 18 is planned into generated video unless the project may show them (Veo needs allow_all for that);
a film is never cut with a text slide where Google refused a shot twice; and the owner can send a film back with
notes and the step to redo (or the shots to remake), applied once by the next tick."""

import json
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit.config import deep_merge, load_pipeline_config
from nraiorbit.demo import OUTLINE, SHOTS, STORY_BIBLE
from nraiorbit.pipeline import OWNER_NOTES, GatePending, Pipeline, PipelineBlocked
from nraiorbit.schemas import Outline, ReworkedShot, ShotList, StoryBible
from nraiorbit.service import produce

VEO = {"providers": {"video": "veo"}, "google": {"person_generation": "allow_adult"}}


def _bible(child_age=8):
    b = StoryBible.model_validate(STORY_BIBLE)
    kid = b.characters[1]
    kid = kid.model_copy(update={"physical": kid.physical.model_copy(update={"age": child_age})})
    return b.model_copy(update={"characters": [b.characters[0], kid, *b.characters[2:]]})


@pytest.fixture
def film(tmp_path):
    root = tmp_path / "rain"
    root.mkdir()
    bible = _bible()
    (root / "01_story_bible.json").write_text(bible.model_dump_json(), encoding="utf-8")
    (root / "02_outline.json").write_text(Outline.model_validate(OUTLINE).model_dump_json(), encoding="utf-8")
    (root / "cast.lock.yaml").write_text(yaml.safe_dump({"lock_version": 1, "characters": {
        c.id: {"character_asset": f"{c.id}_x", "voice_asset": None, "costumes": {}} for c in bible.characters}}))
    (root / "04_shots").mkdir()
    (root / "04_shots" / "sc001.json").write_text(
        ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).model_dump_json(), encoding="utf-8")
    p = Pipeline(root, None, cfg=deep_merge(load_pipeline_config(), VEO), log=lambda *_: None)
    return p, bible


def test_a_child_is_kept_out_of_generated_video_unless_the_project_may_show_them():
    from nraiorbit.characters import minors_off_screen
    bible = _bible()
    kid = bible.characters[1]
    assert minors_off_screen(bible.characters, VEO) == {kid.id: f"{kid.name_en} (8)"}
    assert minors_off_screen(bible.characters, {**VEO, "google": {"person_generation": "allow_all"}}) == {}
    assert minors_off_screen(bible.characters, {"providers": {"video": "mock"}}) == {}
    assert minors_off_screen(_bible(18).characters, VEO) == {}                  # 18 is an adult
    monkey = kid.model_copy(update={"silent_animal": True})                    # an animal's age is not a child's
    assert minors_off_screen([bible.characters[0], monkey], VEO) == {}


def test_the_director_plans_a_child_out_of_frame_and_is_asked_again_when_it_does_not(film):
    p, bible = film
    kid = bible.characters[1].id
    (p.p.root / "04_shots" / "sc001.json").unlink()
    (p.p.root / "03_scenes").mkdir()
    from nraiorbit.demo import SCENES
    from nraiorbit.schemas import SceneScript
    sc = Outline.model_validate(OUTLINE).scenes[0]
    (p.p.root / "03_scenes" / "sc001.json").write_text(SceneScript.model_validate(
        {"scene_id": "sc001", "heading": sc.heading, "synopsis": sc.synopsis, "rasa_primary": sc.rasa_primary,
         "intensity": sc.intensity, "blocks": SCENES["sc001"]}).model_dump_json(), encoding="utf-8")
    p.p.outline.write_text(Outline(scenes=[sc]).model_dump_json(), encoding="utf-8")
    asked = []
    plan = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]})

    def shots(bible_, lock, scene, script, actions, camera, clip_lengths=None, issue="", off_screen=None):
        asked.append((off_screen, issue))
        first = plan.shots[0]
        shown = first.model_copy(update={"characters_in_frame": [kid], "costume_ids": [""]})
        return plan.model_copy(update={"shots": [shown if not issue else first, *plan.shots[1:]]})
    p.director = SimpleNamespace(shots=shots)
    p._shots()
    assert len(asked) == 2 and kid in asked[0][0] and "will not show them" in asked[1][1]
    saved = ShotList.model_validate_json((p.p.root / "04_shots" / "sc001.json").read_text(encoding="utf-8"))
    assert not any(kid in s.characters_in_frame for s in saved.shots)
    from nraiorbit.agents.production import Director
    seen = []
    Director(SimpleNamespace(generate=lambda **kw: seen.append(kw["task"]) or plan), {}).shots(
        bible, p._lock(), sc, SceneScript.model_validate_json((p.p.root / "03_scenes" / "sc001.json").read_text()),
        [], {}, off_screen={kid: "Kid (8)"})
    assert "must not show anyone under 18: Kid (8)" in seen[0]


def test_a_shot_refused_twice_stops_the_video_step_instead_of_a_text_slide(film):
    from nraiorbit.providers import RenderContext
    from nraiorbit.providers.google_ai import ShotFiltered
    p, _ = film
    p._to_look, p._slides = [], []
    shot = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0]
    video = SimpleNamespace(render_shot=lambda *a: (_ for _ in ()).throw(ShotFiltered("refused again")))
    ctx = RenderContext(cast=p._lock(), scene_id="sc001", resolution=(640, 360), fps=24)
    clip = p._render_shot(video, shot, ctx, None)
    assert not clip.exists() and p._slides == [shot.shot_id]                   # no slide file made
    with pytest.raises(PipelineBlocked, match=f"no video, Google's safety filter refused them: {shot.shot_id}"):
        p._stop_for_slides()
    p._slides = []
    p._stop_for_slides()                                                         # none: the run goes on


def test_send_back_with_shots_replans_them_from_the_notes_and_remakes_only_them(film):
    p, bible = film
    root = p.p.root
    first, second = [s.shot_id for s in ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[:2]]
    inbox = root / "inbox" / "video"
    (inbox / ".pending").mkdir(parents=True)
    for name in (f"{first}.mp4", f"{first}_2.mp4", f"{second}.mp4"):
        (inbox / name).write_bytes(b"clip")
    (inbox / ".pending" / f"{first}.json").write_text("{}")
    (root / "06_keyframes" / first).mkdir(parents=True)
    (root / "06_keyframes" / first / "take_1.png").write_bytes(b"png")
    (root / "10_edit").mkdir()
    (root / "10_edit" / "timeline.json").write_text("{}")
    (root / "11_master").mkdir()
    (root / "11_master" / "movie.mp4").write_bytes(b"film")
    (root / "reviews").mkdir()
    for g in ("cost", "faces", "pacing", "trailer", "publish"):
        (root / "reviews" / f"{g}.approved").write_text("ok")
    tuned = {first: {"sync_s": 3.2}, second: {"cut_in_s": 0.5}}
    p.cfg = {**p.cfg, "lipsync": {**(p.cfg.get("lipsync") or {}), "shots": dict(tuned)}}
    (root / "pipeline.yaml").write_text(yaml.safe_dump({"lipsync": {"shots": dict(tuned)}}))
    asked = []

    def rework_shot(bible_, lock, shot, notes, off_screen=None):
        asked.append((shot.shot_id, notes, off_screen))
        return ReworkedShot(characters_in_frame=[bible.characters[0].id, "nobody_by_that_id"], costume_ids=["c1"],
                            image_prompt="the hero alone at the river", motion_prompt="he turns at a voice")
    p.director = SimpleNamespace(rework_shot=rework_shot)
    (root / produce.REWORK_FILE).write_text(json.dumps({"id": "r1", "stage": "render", "shots": [first, "bad id"],
                                                        "notes": "keep the kid out of frame", "by": "red@x"}))
    out = produce.apply_rework(p, log=lambda *_: None)
    assert out["stage"] == "render" and out["shots"] == [first] and asked[0][:2] == (first, "keep the kid out of frame")
    assert bible.characters[1].id in asked[0][2]                               # the director hears who stays out
    plan = ShotList.model_validate_json((root / "04_shots" / "sc001.json").read_text(encoding="utf-8"))
    redone = next(s for s in plan.shots if s.shot_id == first)
    assert redone.characters_in_frame == [bible.characters[0].id] and redone.costume_ids == ["c1"]
    assert redone.image_prompt == "the hero alone at the river"
    # nothing is deleted (the next restore would bring it back): the shot moves on to a take it never had, which
    # the bucket keeps in takes.yaml, so a new first frame and a new clip are made and the old ones stay as history
    assert sorted(x.name for x in inbox.glob("*.mp4")) == sorted([f"{first}.mp4", f"{first}_2.mp4", f"{second}.mp4"])
    assert yaml.safe_load((root / "takes.yaml").read_text()) == {first: 3} and p.takes[first] == 3
    assert p.takes.get(second, 1) == 1                                          # the other shot is untouched
    assert yaml.safe_load((root / "pipeline.yaml").read_text())["lipsync"]["shots"] == {second: {"cut_in_s": 0.5}}
    assert p.cfg["lipsync"]["shots"] == {second: {"cut_in_s": 0.5}}             # tuned to the old clip: dropped
    assert not (root / "10_edit" / "timeline.json").exists() and not (root / "11_master" / "movie.mp4").exists()
    assert (root / "reviews" / "cost.approved").exists() and (root / "reviews" / "faces.approved").exists()
    assert not (root / "reviews" / "trailer.approved").exists() and not (root / "reviews" / "publish.approved").exists()
    assert not (root / produce.REWORK_FILE).exists()
    (root / produce.REWORK_FILE).write_text(json.dumps({"id": "r1", "stage": "render", "shots": [first]}))
    assert produce.apply_rework(p, log=lambda *_: None) is None and len(asked) == 1   # once, by its id


def test_send_back_to_a_step_redoes_it_and_after_and_the_agents_follow_the_notes(film):
    p, _ = film
    root = p.p.root
    (root / "reviews").mkdir()
    (root / "reviews" / "shots.approved").write_text("ok")
    made = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0].shot_id
    (root / "inbox" / "video").mkdir(parents=True)
    (root / "inbox" / "video" / f"{made}.mp4").write_bytes(b"clip")                # a clip of the old plan
    (root / produce.REWORK_FILE).write_text(json.dumps({"id": "r2", "stage": "shots", "notes": "fewer close-ups",
                                                        "by": "red@x"}))
    produce.apply_rework(p, log=lambda *_: None)
    assert not (root / "04_shots").exists() and not (root / "reviews" / "shots.approved").exists()
    assert p.takes == {made: 2}           # the new plan's shot of that id gets a new clip, never the old plan's
    assert p.director.notes == "fewer close-ups" and p.music_director.notes == "fewer close-ups"
    assert p.screenwriter.notes == "" and p.story_analyst.notes == ""          # the steps before it are kept
    rec = json.loads((root / "reports" / OWNER_NOTES).read_text(encoding="utf-8").splitlines()[-1])
    assert rec["id"] == "r2" and rec["stage"] == "shots" and rec["by"] == "red@x"
    again = Pipeline(root, None, cfg=p.cfg, log=lambda *_: None)                 # the next run still follows them
    assert again.director.notes == "fewer close-ups"
    seen = []
    again.director.llm = SimpleNamespace(generate=lambda **kw: seen.append(kw["task"]))
    again.director.ask(ShotList, context="", task="Plan the shots")
    assert "<owner_notes>\nfewer close-ups\n</owner_notes>" in seen[0]


def test_the_tick_hands_the_send_back_to_produce(tmp_path, monkeypatch):
    from nraiorbit.service import queue
    st, rf = str(tmp_path / "stories.csv"), str(tmp_path / "references.csv")
    story = tmp_path / "rain.md"
    story.write_text("A ferryman and a flood.", encoding="utf-8")
    queue.save(st, [{"slug": "rain", "story_uri": str(story), "budget_usd": "40", "gates": "ask", "publish": "none",
                     "state": "retry", "note": "", "updated": ""}], queue.STORY_COLUMNS)
    queue.save(rf, [], queue.REFERENCE_COLUMNS)
    (tmp_path / "rain.rework.json").write_text(json.dumps({"id": "r3", "stage": "trailer", "notes": "shorter"}))
    template = tmp_path / "projects" / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    seen = []

    def produce_fn(root, bucket):
        seen.append(json.loads((root / produce.REWORK_FILE).read_text()))
        return {"state": "awaiting:trailer", "steps": {}}
    queue.tick(st, rf, tmp_path / "projects", bucket=None, produce_fn=produce_fn, learn_fn=None, template=template,
               log=lambda *_: None)
    assert seen == [{"id": "r3", "stage": "trailer", "notes": "shorter"}]


def test_the_control_room_and_the_runner_name_the_same_steps():
    from nraiorbit.ui import films
    assert [k for k, _ in films.REWORK_STEPS] == list(produce.REWORK_STAGES)
    assert set(films.REWORK_DEFAULT.values()) <= set(produce.REWORK_STAGES)
    assert GatePending                                                           # imported for the module's API


def test_face_guard_retakes_start_at_the_shots_current_take(film):
    from nraiorbit.providers import RenderContext
    p, _ = film
    shot = next(s for s in ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots
                if s.characters_in_frame)                                      # the face guard checks faces
    p.takes = {shot.shot_id: 3}
    p.cfg = {**p.cfg, "retakes": {"max_per_shot": 2}}
    seeds = []

    def render(s, clip, ctx):
        seeds.append(ctx.seed)
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"clip")
    p._check_identity = lambda guard, s, clip: SimpleNamespace(passed=len(seeds) == 2, issues=[], to_dict=lambda: {})
    p._report = lambda *a: None
    ctx = RenderContext(cast=p._lock(), scene_id="sc001", resolution=(640, 360), fps=24)
    p._render_shot(SimpleNamespace(render_shot=render), shot, ctx, face_guard=object())
    assert seeds == [3, 4]                                                       # never back to the old take 1


def test_a_new_cut_asks_at_the_film_cut_gate_again(film):
    """Real films stop at final_cut (animatics at pacing): a re-cut must ask again at whichever the film uses."""
    p, _ = film
    (p.p.root / "reviews").mkdir()
    for g in ("pacing", "final_cut", "trailer"):
        (p.p.root / "reviews" / f"{g}.approved").write_text("ok")
    p.invalidate("assemble")
    assert not any((p.p.root / "reviews" / f"{g}.approved").exists() for g in ("pacing", "final_cut", "trailer"))


def test_what_a_redone_step_throws_away_leaves_the_bucket_too(film):
    """The backup only ever adds, so an invalidated trailer would come back with the next fresh container and the
    trailer step would skip: invalidate hands its outputs to remove_remote (set by produce in the runner)."""
    from nraiorbit.ops import backup
    p, _ = film
    handed = []
    p.remove_remote = handed.extend
    p.invalidate("trailer")
    assert p.p.master / "trailer.mp4" in handed
    keys = dict(backup.remote_keys(p.p.root, [
        p.p.master / "trailer.mp4", p.p.master / "stems", p.p.root / "02_outline.json", p.p.root / "04_shots",
        p.p.edit / "timeline.json", p.p.root / "inbox" / "video" / "sc001_s001.mp4", p.p.root / "06_keyframes" / "x",
        p.p.root / "reports" / "cost.jsonl", p.p.root / "reviews" / "trailer.approved", p.p.root / "cache" / "a"]))
    slug = p.p.root.name
    assert keys == {f"{slug}/11_master/trailer.mp4": False, f"{slug}/11_master/stems": True,
                    f"{slug}/project/02_outline.json": False, f"{slug}/04_shots": True}   # never paid media or notes


def test_remove_remote_deletes_files_and_folders_and_shrugs_off_missing_ones(tmp_path, monkeypatch):
    from nraiorbit.ops import backup
    from nraiorbit.release import gcs
    root = tmp_path / "rain"
    objects = {"rain/11_master/trailer.mp4", "rain/11_master/stems/music.wav", "rain/11_master/stems/dialogue.wav",
               "rain/inbox/video/sc001_s001.mp4", "rain/11_master/movie.mp4"}

    class NotFound(Exception):
        pass

    class Blob:
        def __init__(self, name):
            self.name = name

        def delete(self):
            if self.name not in objects:
                raise NotFound(self.name)
            objects.discard(self.name)

    class Bucket:
        def blob(self, key):
            return Blob(key)

        def list_blobs(self, prefix):
            return [Blob(k) for k in sorted(objects) if k.startswith(prefix)]

    monkeypatch.setattr(gcs, "STORAGE_FACTORY", lambda: SimpleNamespace(bucket=lambda name: Bucket()))
    logs = []
    n = backup.remove_remote(root, "b", [root / "11_master" / "trailer.mp4", root / "11_master" / "stems",
                                         root / "11_master" / "reels_gone.mp4", root / "inbox" / "video"], log=logs.append)
    assert n == 3 and objects == {"rain/inbox/video/sc001_s001.mp4", "rain/11_master/movie.mp4"}
    assert "removed 3 old output(s) from the bucket" in logs[-1]
    assert backup.remove_remote(root, None, [root / "11_master" / "movie.mp4"]) == 0                # no bucket


def test_a_music_redo_keeps_every_clip(film):
    p, _ = film
    first = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0].shot_id
    (p.p.root / "inbox" / "video").mkdir(parents=True)
    (p.p.root / "inbox" / "video" / f"{first}.mp4").write_bytes(b"clip")
    (p.p.root / produce.REWORK_FILE).write_text(json.dumps({"id": "m1", "stage": "music_plan", "notes": "softer"}))
    produce.REWORK_STAGES["music_plan"] = "music_plan"                         # as triage will route it
    try:
        produce.apply_rework(p, log=lambda *_: None)
    finally:
        produce.REWORK_STAGES.pop("music_plan")
    assert p.takes == {} and not (p.p.root / "takes.yaml").exists()
