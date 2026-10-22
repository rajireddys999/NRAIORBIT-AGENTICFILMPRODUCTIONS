"""The rehearsal: every stop ahead of a film at once, from the runner's own checks, with no paid call."""

import json

import yaml

from nraiorbit.config import load_pipeline_config
from nraiorbit.demo import OUTLINE, SCENES, SHOTS, STORY_BIBLE
from nraiorbit.library.registry import AssetRegistry
from nraiorbit.pipeline import Pipeline, ProjectPaths
from nraiorbit.schemas import Outline, SceneScript, ShotList, StoryBible
from nraiorbit.service import rehearse


def film(tmp_path, seconds=2.0, **cfg_extra):
    """The demo film planned through its shot lists (cut into 2 s shots, as the live film's first plan was), cast:
    arjun new (no library entry), meera from the library without pictures, bhairava from the library with pictures
    but written as a macaque; real providers, a $1 cap."""
    root = tmp_path / "film"
    paths = ProjectPaths(root)
    bible = StoryBible.model_validate(STORY_BIBLE)
    chars = [c if c.id != "bhairava" else c.model_copy(update={"physical": c.physical.model_copy(update={
        "build": "large macaque with a long tail", "skin_tone": "dark brown fur"})}) for c in bible.characters]
    bible = bible.model_copy(update={"characters": chars})
    root.mkdir(parents=True)
    paths.bible.write_text(bible.model_dump_json(), encoding="utf-8")
    outline = Outline.model_validate(OUTLINE)
    paths.outline.write_text(outline.model_dump_json(), encoding="utf-8")
    paths.scenes.mkdir()
    paths.shots.mkdir()
    for sc in outline.scenes:
        script = SceneScript.model_validate({"scene_id": sc.scene_id, "heading": sc.heading, "synopsis": sc.synopsis,
                                             "rasa_primary": sc.rasa_primary, "intensity": sc.intensity,
                                             "blocks": SCENES[sc.scene_id]})
        (paths.scenes / f"{sc.scene_id}.json").write_text(script.model_dump_json(), encoding="utf-8")
        sl = ShotList.model_validate({"scene_id": sc.scene_id, "shots": [{**s, "duration_s": seconds or s["duration_s"]}
                                                                         for s in SHOTS[sc.scene_id]]})  # as planned before
        (paths.shots / f"{sc.scene_id}.json").write_text(sl.model_dump_json(), encoding="utf-8")
    library = tmp_path / "library"
    for aid, pictured in (("meera_lib", False), ("bhairava_lib", True)):
        folder = library / "characters" / aid
        (folder / "refs" / "face").mkdir(parents=True)
        (folder / "manifest.yaml").write_text(yaml.safe_dump({"id": aid, "kind": "character", "version": 1,
                                                             "license": {"type": "owned", "commercial_use": True},
                                                             "files": {"face_refs": "refs/face/"}}))
        if pictured:
            (folder / "refs" / "face" / "01_front.png").write_bytes(b"png")
    paths.lock.write_text(yaml.safe_dump({"characters": {
        "arjun": {"character_asset": "characters/arjun@pending"},
        "meera": {"character_asset": "characters/meera_lib@v1"},
        "bhairava": {"character_asset": "characters/bhairava_lib@v1"}}}))
    cfg = {"providers": {"video": "veo", "tts": "inbox", "music": "mock", "lipsync": "sync_so"},
           "voice": {"inbox_fallback": "elevenlabs"}, "budget": {"max_usd": 1},
           "policy": {"gates": "ask", "publish": "private"}, **cfg_extra}
    (root / "pipeline.yaml").write_text(yaml.safe_dump(cfg))
    return Pipeline(root, None, cfg=load_pipeline_config(root), library=AssetRegistry.load(library), log=lambda *_: None)


def said(found, kind):
    return " | ".join(f"[{f['step']}] {f['what']} -> {f['fix']}" for f in found[kind])


def test_a_rehearsal_finds_every_stop_ahead_at_once(tmp_path):
    found = rehearse.rehearse(film(tmp_path))
    stops = said(found, "stops")
    assert "no reference pictures for arjun, meera" in stops and "build_identity: true" in stops
    assert "no ElevenLabs voice for arjun, meera" in stops and "voice.auto_cast" in stops
    assert "bhairava is an animal with lines and no voice" in stops and "rewrite the lines as sound" in stops
    assert "over the cap $1.00" in stops                                     # the cost gate, asked as the run will
    waste = said(found, "waste")
    assert "shots are shorter than the 4/6/8 s clips Veo bills" in waste and "billable_grid: true" in waste
    assert "lip-sync would run on an animal's face in 2 shots: sc001_s003, sc004_s002" in waste
    assert "lipsync: {shots: {<shot id>: {sync: false}}}" in waste          # the setting the pipeline reads
    waits = next(f["what"] for f in found["notes"] if f["what"].startswith("waits for you at"))
    assert "cost" in waits and "faces" in waits and "final_cut" in waits and "publish" in waits
    assert "pacing" not in waits                                          # a real film meets the final cut, not pacing
    assert found["estimate"]["video_usd"] > 1 and found["estimate"]["total_usd"] >= found["estimate"]["video_usd"]


def test_what_the_runner_does_on_its_own_is_a_note_not_a_stop(tmp_path):
    found = rehearse.rehearse(film(tmp_path, cast={"build_identity": True}, budget={"max_usd": 500},
                                   voice={"inbox_fallback": "elevenlabs", "auto_cast": True},
                                   shots={"billable_grid": True}, policy={"gates": "auto", "publish": "none"}))
    stops, notes = said(found, "stops"), said(found, "notes")
    assert "pictures" not in stops and "the cast step makes reference pictures for arjun, meera: about 6 pictures" in notes
    assert "no ElevenLabs voice for" not in stops and "picks ElevenLabs voices for arjun, meera" in notes
    assert "bhairava is an animal" in stops                                  # an animal is always the owner's call
    assert "the paid stages: about $" in notes and "cap $500.00" in notes
    assert "re-plan the shots" in said(found, "waste") and "billable_grid" not in said(found, "waste")
    assert "no YouTube upload: publish is none" in notes and "waits for you at: cost" in notes


def test_an_unplanned_film_says_what_claude_writes_next_and_the_text_report_reads_in_order(tmp_path):
    p = film(tmp_path)
    for f in p.p.shots.glob("*.json"):
        f.unlink()
    found = rehearse.rehearse(p)
    assert "not planned yet: shot lists" in said(found, "notes") and "over the cap" not in said(found, "stops")
    text = rehearse.report_text(found)
    assert text.index("Stops") < text.index("Notes") and "→" in text
    assert rehearse.report_text({"error": "x"}) == "rehearsal failed: x"


def test_every_run_leaves_its_rehearsal_and_a_failure_never_ends_the_run(tmp_path):
    p = film(tmp_path)
    assert rehearse.save(p)["stops"]
    assert json.loads((p.p.reports / "rehearsal.json").read_text())["stops"]
    from types import SimpleNamespace
    stand_in = SimpleNamespace(p=ProjectPaths(tmp_path / "other"), cfg={})       # no plan, no pricing
    assert "error" in rehearse.save(stand_in)
    assert "error" in json.loads((tmp_path / "other" / "reports" / "rehearsal.json").read_text())


def test_the_rehearse_command_prints_the_report(tmp_path, monkeypatch, capsys):
    from nraiorbit import cli
    p = film(tmp_path)
    monkeypatch.setattr(cli, "PROJECTS", p.p.root.parent)
    from nraiorbit.library import registry
    monkeypatch.setattr(registry.AssetRegistry, "load", classmethod(lambda cls, root: p.library))
    cli.main(["rehearse", "film"])
    out = capsys.readouterr().out
    assert out.startswith("Stops") and "no reference pictures for arjun, meera" in out



def test_only_the_gates_a_run_will_stop_at_are_listed(tmp_path):
    from nraiorbit.service import produce
    p = film(tmp_path, budget={"max_usd": 500})
    total = rehearse.rehearse(p)["estimate"]["total_usd"]
    produce.approve(p, "cost", "approved\n" + json.dumps({"total_usd": total}) + "\n")
    waits = next(f["what"] for f in rehearse.rehearse(p)["notes"] if f["what"].startswith("waits for you"))
    assert "cost" not in waits                                               # approved at this figure
    produce.approve(p, "cost", "approved\n" + json.dumps({"total_usd": total / 3}) + "\n")
    waits = next(f["what"] for f in rehearse.rehearse(p)["notes"] if f["what"].startswith("waits for you"))
    assert "cost" in waits                                                   # the plan grew since: asked again
    p.p.master.mkdir(parents=True, exist_ok=True)
    p.p.reports.mkdir(parents=True, exist_ok=True)
    (p.p.master / "movie.mp4").write_bytes(b"this cut")
    (p.p.reports / "youtube.json").write_text(json.dumps({"film": {"id": "v1",
                                                                  "file": produce.fingerprint(p.p.master / "movie.mp4")}}))
    waits = next(f["what"] for f in rehearse.rehearse(p)["notes"] if f["what"].startswith("waits for you"))
    assert "publish" not in waits                                            # this very cut is up
    (p.p.master / "movie.mp4").write_bytes(b"a new cut")
    waits = next(f["what"] for f in rehearse.rehearse(p)["notes"] if f["what"].startswith("waits for you"))
    assert "publish" in waits                                                # a new cut asks again


def test_shots_longer_than_a_clip_are_a_note_and_waste_only_counts_seconds_cut_away(tmp_path):
    found = rehearse.rehearse(film(tmp_path, seconds=None))                 # the demo's own 6 to 20 s shots
    assert not any("shorter than the" in f["what"] for f in found["waste"])   # nothing generated is cut away
    long = next(f["what"] for f in found["notes"] if "longer than Veo's 8 s clip" in f["what"])
    assert "held or slowed in the edit" in long
