"""Stage orchestrator: story -> screenplay -> cast -> shots -> music -> render -> edit -> trailer.

Every stage writes its artifact into the project directory and is skipped on re-run if
the artifact exists, so a crash or a review gate never loses work. `force` invalidates
a stage and everything downstream of it. In production the same stages run as Temporal
activities (docs/INFRA_COST_ROADMAP.md); the contracts do not change.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import numpy as np
import yaml
from pydantic import BaseModel

from .agents import CastingAgent, DialogueCritic, Director, Editor, MusicDirector, Screenwriter, StoryAnalyst
from .agents.production import least_shots, most_shots, shots_fit
from .concurrency import ordered_map, workers
from .config import REPO_ROOT, load_navarasa, load_pipeline_config
from .costing import BudgetExceeded, SpendLedger, crowd_voices, load_pricing, load_takes
from .guards.identity import IdentityGuard, IdentityThresholds
from .guards.voice import VoiceGuard, VoiceTake, VoiceThresholds
from .library.registry import AssetRegistry
from .llm import LLM
from .planning.music import apply_revisions, plan_cues
from .planning.score import load_styles, score_cue
from .planning.runtime import (
    TEMPLATES, describe_template, fit_shots_to_grid, fit_shots_to_scene, rebalance_outline, scene_start_times,
    validate_outline,
)
from .post import assemble, facelock, ffmpeg
from .post.cards import Card, apply_cards
from .post.speech import clarify_dialogue, level_gain_db
from .post.subtitles import write_srt
from .post.trailer import PUNCH_DROP_S, TrailerLine, elevation_arc, plan_trailer, unmet
from .providers import RenderContext, get_provider
from .providers.google_ai import FirstFrameToLookAt, GoogleAPIError, ShotFiltered
from .providers.mock import MockTTS, MockVideo
from .shotpack import clips_for, selected
from .schemas import CastLock, Critique, Cue, CueSheet, Outline, SceneScript, ShotList, StoryBible, TrailerDirection

STAGES = ["story_bible", "outline", "scenes", "cast", "shots", "music_plan", "render", "assemble", "trailer"]
OWNER_NOTES = "owner_notes.jsonl"            # reports/: every Send back from the control room, the latest one in force
EDL = "edl.json"                             # reports/: where every shot and line sits in the cut (10_edit is not kept)
TRAILER_PLAN = "trailer_plan.json"           # reports/: the trailer as cut, kept for the editor
TRAILER_DIRECTION = "trailer_direction.json"  # reports/: the owner's wishes for the trailer, from the editor
OWNER_MIX = "owner_mix.json"                 # reports/: the owner's change to the mix (music level, lines over music)
STAGE_GATES = {"story_bible": "story", "outline": "outline", "scenes": "scenes", "cast": "cast", "shots": "shots",
               "render": "render", "assemble": "pacing", "trailer": "trailer"}


def gates_of(stage: str) -> tuple[str, ...]:
    """Every review gate a stage can raise: the cut is `pacing` for an animatic and `final_cut` for a film."""
    if stage == "assemble":
        return ("pacing", "final_cut")
    return (STAGE_GATES[stage],) if stage in STAGE_GATES else ()
# The step gates sit between stages (run() raises them after the stage) and fire only when the project's
# review_gates lists them: the control room's template does, so a queued film stops at every step; a project that
# never mentions them (the CLI's defaults, the demo configs) runs through as before.
STEP_GATES = {"outline", "scenes", "shots", "render"}
DIALOGUE_LEAD_IN_S = 0.2
DIALOGUE_GAP_S = 0.25        # breath between consecutive lines


class GatePending(Exception):
    def __init__(self, gate: str, project_dir: Path):
        self.gate = gate
        super().__init__(f"review gate '{gate}' is waiting for approval in {project_dir}")


class PipelineBlocked(Exception):
    pass


SAFETY_REWRITES = "safety_rewrites.jsonl"


def safer_shot_fn(root: Path, director: Callable[[], Director], log=print):
    """What the video provider calls when Google refuses a shot for safety: the director words the shot again to show
    the same moment without what the filter blocks; the shot list is saved with it (the frame, the clip and the
    owner's view all use the new words) and reports/safety_rewrites.jsonl keeps both wordings. None when the shot's
    words are a rewrite already or the rewrite fails, so the refusal stands and goes to the review queue."""
    reports = Path(root) / "reports"

    def earlier(done: Path) -> list[dict]:
        rows = []
        for line in done.read_text(encoding="utf-8").splitlines() if done.exists() else []:
            try:
                rows.append(json.loads(line))
            except ValueError:                             # a line cut short by a killed run: skip it
                continue
        return rows

    def safer(shot, scene_id: str, refusal: str):
        done = reports / SAFETY_REWRITES
        if any(r.get("shot_id") == shot.shot_id and (r.get("after") or {}).get("image_prompt") == shot.image_prompt
               for r in earlier(done)):                     # these words are the rewrite already (a re-plan is not)
            log(f"  {shot.shot_id}: refused again after it was worded again; it goes to the review queue")
            return None
        path = Path(root) / "04_shots" / f"{scene_id}.json"
        try:
            bible = StoryBible.model_validate_json((Path(root) / "01_story_bible.json").read_text(encoding="utf-8"))
            new = director().safer_shot(bible, shot, refusal)
            shots = ShotList.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:                              # the refusal stands; the run is not stopped by this
            log(f"  {shot.shot_id}: refused for safety, and it could not be worded again ({type(e).__name__}: {e})")
            return None
        words = {"image_prompt": new.image_prompt, "motion_prompt": new.motion_prompt}
        if not any(s.shot_id == shot.shot_id for s in shots.shots):
            log(f"  {shot.shot_id}: refused for safety, and it is not in {path.name} to be worded again")
            return None
        # only the words change in the shot list: the shot in hand may be trimmed for the edit (duration_s)
        path.write_text(shots.model_copy(update={"shots": [s.model_copy(update=words) if s.shot_id == shot.shot_id
                                                           else s for s in shots.shots]}).model_dump_json(indent=2),
                        encoding="utf-8")
        fixed = shot.model_copy(update=words)
        reports.mkdir(parents=True, exist_ok=True)
        cut = done.exists() and not done.read_text(encoding="utf-8").endswith("\n")   # a line a killed run left
        with done.open("a", encoding="utf-8") as f:
            f.write(("\n" if cut else "") + json.dumps({"shot_id": shot.shot_id, "refusal": refusal[:300],
                                "before": {"image_prompt": shot.image_prompt, "motion_prompt": shot.motion_prompt},
                                "after": {"image_prompt": fixed.image_prompt, "motion_prompt": fixed.motion_prompt}},
                               ensure_ascii=False) + "\n")
        log(f"  {shot.shot_id}: refused by Google's safety filter; worded again and tried once more")
        return fixed

    return safer


class ProjectPaths:
    def __init__(self, root: Path):
        self.root = root
        self.story_input = root / "input" / "story.md"
        self.bible = root / "01_story_bible.json"
        self.outline = root / "02_outline.json"
        self.scenes = root / "03_scenes"
        self.briefs = root / "04_identity_briefs"
        self.shots = root / "04_shots"
        self.lock = root / "cast.lock.yaml"
        self.dialogue = root / "05_dialogue"
        self.video = root / "07_video"
        self.music = root / "08_music"
        self.edit = root / "10_edit"
        self.master = root / "11_master"
        self.reports = root / "reports"
        self.reviews = root / "reviews"

    def stage_outputs(self, stage: str) -> list[Path]:
        return {
            "story_bible": [self.bible],
            "outline": [self.outline],
            "scenes": [self.scenes],
            "cast": [self.lock, self.briefs],
            "shots": [self.shots],
            "music_plan": [self.music / "cue_sheet.json"],
            "render": [self.edit / "timeline.json", self.dialogue, self.video]
                      + sorted(self.music.glob("cue*.wav")),
            "assemble": [self.master / "movie.mp4", self.edit / "film_dialogue.mp4", self.edit / "scenes",
                         self.master / "stems", self.edit / "movie_nocards.mp4", self.edit / "cards.json",
                         self.edit / "cards", self.master / "deliver"],      # a new cut never ships an old 720p copy
            "trailer": [self.master / "trailer.mp4", self.edit / "trailer_plan.json"],
        }[stage]


class Pipeline:
    def __init__(self, project_dir: Path, llm: LLM, *, cfg: dict | None = None, navarasa: dict | None = None,
                 library: AssetRegistry | None = None, log: Callable[[str], None] = print):
        self.p = ProjectPaths(Path(project_dir))
        self.cfg = cfg or load_pipeline_config(self.p.root)
        self.navarasa = navarasa or load_navarasa()
        self.library = library or AssetRegistry.load(REPO_ROOT / "library")
        self.pricing = load_pricing(self.p.root)
        self.ledger = SpendLedger(self.p.reports / "cost.jsonl", self.cfg.get("budget", {}).get("max_usd"))
        self.face_detector = None                      # lip-sync speaker lock; tests inject a fake
        self.face_mesh = None                          # lipsync.shots.<id>.mesh; tests inject a fake
        self.takes = load_takes(self.p.root)
        self.look_at_new_frames = False                 # produce sets it when the faces gate is on (ask)
        self.remove_remote = None                       # produce sets it: invalidated outputs leave the bucket too
        self.log = log
        self.on_stage = None                           # called with each stage's name as it starts (live progress)
        self.story_analyst = StoryAnalyst(llm, self.cfg)
        self.screenwriter = Screenwriter(llm, self.cfg)
        self.critic = DialogueCritic(llm, self.cfg)
        self.casting = CastingAgent(llm, self.cfg)
        self.director = Director(llm, self.cfg)
        self.music_director = MusicDirector(llm, self.cfg)
        self.editor = Editor(llm, self.cfg)            # the owner's rejection of the cut or the trailer (service/triage)
        self._owner_notes()

    # ------------------------------------------------------------------ driver

    def _owner_notes(self) -> None:
        """The latest Send back's notes (reports/owner_notes.jsonl) go to the agents of the step it named and every
        step after it, so whatever is planned again follows them."""
        path = self.p.reports / OWNER_NOTES
        latest = None
        for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
            try:
                latest = json.loads(line)
            except ValueError:
                continue
        if not latest or not latest.get("notes") or latest.get("stage") not in STAGES:
            return
        agents = {"story_bible": [self.story_analyst], "outline": [self.screenwriter],
                  "scenes": [self.screenwriter, self.critic], "cast": [self.casting], "shots": [self.director],
                  "music_plan": [self.music_director]}
        for s in STAGES[STAGES.index(latest["stage"]):]:
            for agent in agents.get(s, []):
                agent.notes = latest["notes"]

    def fresh_takes(self, shot_ids) -> dict[str, int]:
        """Each shot moves on to a take it has never had (one past every clip, pending job and first frame it has),
        written to takes.yaml, which the bucket keeps: the next restore brings the old clips and stills back, and they
        stay as history, but the video step draws a new first frame and makes a new clip. Its lip-sync settings, tuned
        to the old clip, are dropped."""
        import re as _re
        moved = {}
        inbox = self.p.root / "inbox" / "video"
        for sid in shot_ids:
            clip = _re.compile(rf"{_re.escape(sid)}(?:_(\d+))?\.(?:mp4|json)")
            used = [int(m.group(1) or 1) for f in [*inbox.glob(f"{sid}*.mp4"), *(inbox / ".pending").glob(f"{sid}*.json")]
                    if (m := clip.fullmatch(f.name))]
            used += [int(m.group(1)) for f in (self.p.root / "06_keyframes" / sid).glob("take_*.png")
                     if (m := _re.fullmatch(r"take_(\d+)\.png", f.name))]
            if used:                        # nothing made yet: its take is fresh already
                moved[sid] = max([self.takes.get(sid, 1), *used]) + 1
        if moved:
            path = self.p.root / "takes.yaml"
            takes = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
            takes.update(moved)
            path.write_text(yaml.safe_dump(takes, sort_keys=True), encoding="utf-8")
            self.takes.update(moved)
        cfg_path = self.p.root / "pipeline.yaml"
        tuned = ((self.cfg.get("lipsync") or {}).get("shots") or {})
        if any(s in tuned for s in shot_ids):
            for s in shot_ids:
                tuned.pop(s, None)
            if cfg_path.exists():
                own = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                for s in shot_ids:
                    ((own.get("lipsync") or {}).get("shots") or {}).pop(s, None)
                cfg_path.write_text(yaml.safe_dump(own, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return moved

    def made_shots(self) -> list[str]:
        """Every shot id that has a clip, a pending job or a first frame."""
        import re as _re
        inbox = self.p.root / "inbox" / "video"
        ids = {m.group(1) for p in [*inbox.glob("*.mp4"), *(inbox / ".pending").glob("*.json")]
               if (m := _re.match(r"(sc\d{3}_s\d{3})", p.name))}
        frames = self.p.root / "06_keyframes"
        ids |= {p.name for p in frames.iterdir() if p.is_dir()} if frames.is_dir() else set()
        return sorted(ids)

    def rework_shots(self, shot_ids: list[str], notes: str) -> list[str]:
        """Shots the owner sent back: planned again from the notes (when there are notes) and moved to a fresh take
        (fresh_takes), so the video step makes them anew; every other shot and everything paid is kept. Returns the
        shots found."""
        from .characters import minors_off_screen
        bible = self._load(self.p.bible, StoryBible)
        lock = self._lock()
        known = {c.id for c in bible.characters}
        off_screen = minors_off_screen(bible.characters, self.cfg)
        found, plans = [], []
        for path in sorted(self.p.shots.glob("*.json")):
            plan = self._load(path, ShotList)
            changed = False
            for i, shot in enumerate(plan.shots):
                if shot.shot_id not in shot_ids:
                    continue
                found.append(shot.shot_id)
                if notes:
                    new = self.director.rework_shot(bible, lock, shot, notes, off_screen)
                    cast = [c for c in new.characters_in_frame if c in known]
                    costumes = (list(new.costume_ids) + [""] * len(cast))[:len(cast)]
                    plan.shots[i] = shot.model_copy(update={
                        "characters_in_frame": cast, "costume_ids": costumes,
                        "image_prompt": new.image_prompt, "motion_prompt": new.motion_prompt})
                    changed = True
            if changed:
                plans.append((path, plan))
        for path, plan in plans:            # every shot is planned again before any is written: a failure part-way
            self._save(path, plan)          # leaves no shot with new words and its old clip
        if found:                           # the cut is made again from the new clips; nothing paid is lost
            self.fresh_takes(found)
            (self.p.edit / "timeline.json").unlink(missing_ok=True)
            self.invalidate("assemble")
        return found

    def run(self, until: str | None = None, force: tuple[str, ...] = ()) -> None:
        if force:
            self.invalidate(min(force, key=STAGES.index))
        for stage in STAGES:
            self.log(f"[{stage}]")
            if self.on_stage is not None:
                self.on_stage(stage)
            getattr(self, f"_{stage}")()
            if STAGE_GATES.get(stage) in STEP_GATES:
                self.gate(STAGE_GATES[stage])
            if stage == until:
                return

    def invalidate(self, stage: str) -> None:
        gone = []
        for s in STAGES[STAGES.index(stage):]:
            for path in self.p.stage_outputs(s):
                gone.append(path)
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            for gate in gates_of(s):
                for suffix in ("approved", "pending"):
                    (self.p.reviews / f"{gate}.{suffix}").unlink(missing_ok=True)
        remover = getattr(self, "remove_remote", None)   # the bucket's copies too, or the next fresh container
        if remover is not None:                           # restores them and the step skips
            remover(gone)
        # the runner's own gates (service/produce) answer for what follows: a replan or a re-render needs the cost
        # and the first frames approved again; a new cut needs the upload approved again
        for name, upto in (("cost", "render"), ("faces", "render"), ("publish", "trailer")):
            if STAGES.index(stage) <= STAGES.index(upto):
                for suffix in ("approved", "pending"):
                    (self.p.reviews / f"{name}.{suffix}").unlink(missing_ok=True)
        if STAGES.index(stage) <= STAGES.index("trailer"):
            (self.p.reports / "youtube.json").unlink(missing_ok=True)      # a new cut is a new upload
        direction = self.p.reports / TRAILER_DIRECTION
        if STAGES.index(stage) <= STAGES.index("shots") and direction.exists():
            # its shot and line ids name the old plan's; the new plan reuses them for other shots. Overwritten, not
            # deleted: the bucket keeps reports/ and a restore would bring it back
            direction.write_text(json.dumps({"direction": None, "cleared": f"{stage} was planned again"}),
                                 encoding="utf-8")

    def status(self) -> dict[str, str]:
        out = {}
        for s in STAGES:
            done = all(p.exists() for p in self.p.stage_outputs(s)[:1])
            gate = next((g for g in gates_of(s) if (self.p.reviews / f"{g}.pending").exists()), None)
            if gate:
                out[s] = f"waiting for review '{gate}'"
            else:
                out[s] = "done" if done else "todo"
        return out

    def gate(self, name: str) -> None:
        gates = self.cfg.get("review_gates") or {}
        if name in STEP_GATES and name not in gates:          # a step gate the project never asked for
            return
        g = gates.get(name) or {}
        if not g.get("enabled", True) or g.get("auto_approve", False):
            return
        if (self.p.reviews / f"{name}.approved").exists():
            return
        self.p.reviews.mkdir(parents=True, exist_ok=True)
        (self.p.reviews / f"{name}.pending").write_text(
            f"Review the '{name}' artifacts, then run: nraiorbit approve {self.p.root.name} {name}\n", encoding="utf-8")
        raise GatePending(name, self.p.root)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _save(path: Path, model: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.model_dump_json(indent=2), encoding="utf-8")

    @staticmethod
    def _load(path: Path, cls):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def _report(self, name: str, record: dict) -> None:
        self.p.reports.mkdir(parents=True, exist_ok=True)
        with open(self.p.reports / f"{name}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _lock(self) -> CastLock:
        return CastLock.model_validate(yaml.safe_load(self.p.lock.read_text(encoding="utf-8")))

    @property
    def _film(self) -> dict:
        return self.cfg["film"]

    def _animatic(self) -> bool:
        prov = self.cfg["providers"]
        return all(prov.get(k) in ("mock", "none") for k in ("keyframe", "video", "tts", "music"))

    # ------------------------------------------------------------------ S1 story bible

    def _story_bible(self) -> None:
        if not self.p.bible.exists():
            plot = self.p.story_input.read_text(encoding="utf-8")
            runtime, template = self._film["runtime_minutes"], self._film["structure_template"]
            names = list(TEMPLATES) if template == "auto" else [template]
            guide = "\n\n".join(describe_template(t, runtime) for t in names)
            bible = self.story_analyst.run(plot, runtime, template, guide)
            self._save(self.p.bible, bible)
            self.log(f"  story bible: {bible.title_en} / {bible.title_te}, {len(bible.characters)} characters")
        self.gate("story")

    # ------------------------------------------------------------------ S2 outline

    def _outline(self) -> None:
        if self.p.outline.exists():
            return
        bible = self._load(self.p.bible, StoryBible)
        runtime, tol = self._film["runtime_minutes"], self._film["runtime_tolerance_minutes"]
        guide = describe_template(bible.structure_template, runtime)
        known = {c.id for c in bible.characters}

        def check(o: Outline) -> list[str]:
            issues = validate_outline(o, runtime, tol)
            for sc in o.scenes:
                issues += [f"{sc.scene_id}: unknown character '{c}'" for c in sc.characters if c not in known]
            return issues

        grid = self._grid_lengths()                       # scene targets the clips can add up to: even for 4, 6
        step = math.gcd(*grid) if grid else 1              # and 8 s, a multiple of 8 when 8 s is the only length
        outline = rebalance_outline(self.screenwriter.outline(bible, runtime, guide), runtime, step=step)
        issues = check(outline)
        if issues:
            self.log(f"  outline issues, asking for a revision: {issues}")
            outline = rebalance_outline(self.screenwriter.outline(bible, runtime, guide, issues=issues), runtime,
                                        step=step)
            issues = check(outline)
        for issue in issues:
            self._report("outline_issues", {"issue": issue})
        self._save(self.p.outline, outline)
        self.log(f"  outline: {len(outline.scenes)} scenes, {sum(s.target_seconds for s in outline.scenes) / 60:.1f} min")

    # ------------------------------------------------------------------ S3 scenes

    def _scenes(self) -> None:
        bible = self._load(self.p.bible, StoryBible)
        outline = self._load(self.p.outline, Outline)
        known = {c.id for c in bible.characters}
        rounds = self.cfg["llm"].get("critique_rounds", 2)
        pass_score = self.cfg["llm"].get("critique_pass_score", 8)
        previous = None
        for sc in outline.scenes:
            path = self.p.scenes / f"{sc.scene_id}.json"
            if path.exists():
                previous = self._load(path, SceneScript).synopsis
                continue
            script = self.screenwriter.scene(bible, outline, sc, previous)
            for _ in range(rounds):
                critique = self.critic.review(bible, script)
                self._report("critiques", {"scene_id": sc.scene_id, **critique.model_dump()})
                if critique.score >= pass_score:
                    break
                script = self.screenwriter.revise(bible, outline, script, critique)
            problems = self._script_problems(script, known)
            if problems:
                fix = Critique(score=0, issues=problems,
                               revision_instructions=["Every dialogue block needs a character_id from the Story Bible."])
                script = self.screenwriter.revise(bible, outline, script, fix)
                problems = self._script_problems(script, known)
                if problems:
                    raise PipelineBlocked(f"{sc.scene_id}: {problems}")
            script = self._animal_lines_as_sound(script.model_copy(update={"scene_id": sc.scene_id}), bible)
            self._save(path, script)
            previous = script.synopsis
            self.log(f"  {sc.scene_id}: {sum(b.kind == 'dialogue' for b in script.blocks)} lines")

    def _animal_lines_as_sound(self, script: SceneScript, bible: StoryBible) -> SceneScript:
        """An animal's "lines" are its sounds: an action line, never dialogue, so no voice reads a description aloud
        and no lip-sync is paid on an animal's face. Only characters the story bible marks silent_animal: true;
        voice.animals_speak (true, or the ids that talk) keeps a talking animal's lines, whose voice is then set on
        its cast card."""
        from .characters import silent_animals
        animals = silent_animals(bible.characters, self.cfg)

        def spoken(b) -> bool:
            return b.kind == "dialogue" and b.character_id in animals

        def as_sound(b):
            text = f"{animals[b.character_id]} makes animal sounds and gestures, no words (meaning: {b.text_en})"
            return b.model_copy(update={"kind": "action", "character_id": None, "delivery": None,
                                        "text_en": text, "text_te": text})       # nothing left for a voice to read
        n = sum(map(spoken, script.blocks))
        if not n:
            return script
        blocks = [as_sound(b) if spoken(b) else b for b in script.blocks]
        self.log(f"  {script.scene_id}: {n} animal line(s) kept as sound, not speech")
        return script.model_copy(update={"blocks": blocks})

    @staticmethod
    def _script_problems(script: SceneScript, known: set[str]) -> list[str]:
        problems = []
        for b in script.blocks:
            if b.kind == "dialogue" and b.character_id not in known:
                problems.append(f"{b.block_id}: speaker '{b.character_id}' not in Story Bible")
        return problems

    # ------------------------------------------------------------------ S4 cast

    def _cast(self) -> None:
        if not self.p.lock.exists():
            bible = self._load(self.p.bible, StoryBible)
            production_only = not self._animatic()
            lock, briefs = self.casting.cast(bible, self.library, production_only=production_only)
            for brief in briefs:
                self._save(self.p.briefs / f"{brief.character_id}.json", brief)
            self.p.lock.write_text(yaml.safe_dump(lock.model_dump(), sort_keys=False, allow_unicode=True),
                                   encoding="utf-8")
            for cid, entry in lock.characters.items():
                self.log(f"  {cid} -> {entry.character_asset} / voice {entry.voice_asset}")
        prov = self.cfg.get("providers") or {}
        makes_pictures = any(prov.get(k, "mock") not in ("mock", "none") for k in ("keyframe", "video"))
        if makes_pictures and (self.cfg.get("cast") or {}).get("build_identity"):
            self._build_cast()                            # the pictures feed first frames and video, nothing else
        if (self.cfg.get("voice") or {}).get("auto_cast"):
            self._cast_voices()                           # before the gate, which shows them
        self.gate("cast")
        pending = [cid for cid, e in self._lock().characters.items() if e.character_asset.endswith("@pending")]
        if pending and not self._animatic():
            raise PipelineBlocked(
                f"characters {pending} need identity packs (briefs in {self.p.briefs}). "
                "Build the packs, register them in library/, then re-run with --force cast.")

    def _build_cast(self) -> None:
        """A real film's characters need reference pictures before any video is paid, so faces hold from shot to
        shot. A character no library character fits gets a library entry of its own from the story bible; every cast
        character still without pictures gets them (Gemini images, a few cents each, under the cap). The cast gate
        then shows them. Pictures already there are kept: this costs nothing a second time."""
        from .identity import build_identity_pack, images_to_make, plan_identity_pack, register_new_characters

        bible = self._load(self.p.bible, StoryBible)
        lock, self.library, added = register_new_characters(bible, self._lock(), self.library, film=self.p.root.name,
                                                            log=self.log)
        if added:
            self.p.lock.write_text(yaml.safe_dump(lock.model_dump(), sort_keys=False, allow_unicode=True),
                                   encoding="utf-8")
        if images_to_make(plan_identity_pack(bible, lock, self.library, log=lambda m: None)):
            build_identity_pack(bible, lock, self.library, self._identity_images(), log=self.log)
        missing = [job.spec.id for job in plan_identity_pack(bible, lock, self.library, log=lambda m: None)
                   if not job.front.exists()]
        if missing:
            raise PipelineBlocked(f"characters {missing} have no portrait: the image model refused or failed. "
                                  "Soften their description in the story bible, then retry.")

    def _cast_voices(self) -> None:
        """voice.auto_cast: every speaking character ElevenLabs could not voice gets one from the account's voices,
        matched to the story bible's gender, age and timbre, a different voice each. The picks go into the project's
        voice.cast and reports/voices.json (what was picked and why), for the cast gate. A voice the owner set is
        never replaced; when the account cannot be listed, nothing is picked and the voices check stops the film
        before anything is paid."""
        from collections import Counter

        from .identity import is_animal
        from .providers.elevenlabs import account_voices, pick_voices, want_for
        need = self._voiceless()
        if not need:
            return
        v = self.cfg.setdefault("voice", {})
        try:
            voices = account_voices(api_key_env=(v.get("elevenlabs") or {}).get("api_key_env", "ELEVENLABS_API_KEY"))
        except Exception as exc:
            self.log(f"  voices: the ElevenLabs account's voices could not be listed ({type(exc).__name__}): {exc}")
            return
        bible = self._load(self.p.bible, StoryBible)
        specs = {c.id: c for c in bible.characters}
        lines = Counter(b.character_id for path in self.p.scenes.glob("*.json")
                        for b in SceneScript.model_validate_json(path.read_text(encoding="utf-8")).blocks
                        if b.kind == "dialogue")
        animals = sorted(c for c in need if c in specs and is_animal(specs[c]))
        if animals:                                       # chatter is no line for a voice to read: the owner decides
            self.log(f"  voices: {animals} are animals and get no voice of their own; set one on the cast card, put "
                     "recordings in inbox/dialogue, or rewrite their lines as sound")
        order = sorted((c for c in need if c in specs and c not in animals), key=lambda c: (-lines[c], c))
        picks = pick_voices([(c, want_for(specs[c], animal=is_animal(specs[c]))) for c in order], voices,
                            taken=set((v.get("cast") or {}).values()))
        if not picks:
            self.log("  voices: the ElevenLabs account has no voices to cast")
            return
        path = self.p.root / "pipeline.yaml"
        disk = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}) or {}
        disk["voice"] = disk.get("voice") if isinstance(disk.get("voice"), dict) else {}   # an empty `voice:` too
        cast = disk["voice"].get("cast") if isinstance(disk["voice"].get("cast"), dict) else {}
        disk["voice"]["cast"] = cast
        for cid, pick in picks.items():
            cast.setdefault(cid, pick["voice_id"])
        path.write_text(yaml.safe_dump(disk, sort_keys=False, allow_unicode=True), encoding="utf-8")
        v["cast"] = {**(v.get("cast") or {}), **{c: cast[c] for c in picks}}
        self.p.reports.mkdir(parents=True, exist_ok=True)
        record = self.p.reports / "voices.json"
        before = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {}
        record.write_text(json.dumps({**before, **picks}, indent=1, ensure_ascii=False), encoding="utf-8")
        for cid, pick in picks.items():
            self.log(f"  {cid}: ElevenLabs voice {pick['name']} ({pick['why']}){' · shared' if pick['shared'] else ''}")

    def _voiceless(self) -> list[str]:
        """Speaking characters ElevenLabs could not voice, checked before the cost gate: speech runs after each
        scene's clips, so a missing voice would stop the film with video already paid. Counted: dialogue lines some
        shot carries, that ElevenLabs speaks (directly, or for a line without a hand-made recording in inbox/dialogue;
        a chant goes to voice.crowd_provider), whose speaker neither voice.cast, the character's library voice nor
        voice.elevenlabs.default_voice gives a voice."""
        prov, v = self.cfg.get("providers") or {}, self.cfg.get("voice") or {}
        if not (prov.get("tts") == "elevenlabs" or (prov.get("tts") == "inbox" and v.get("inbox_fallback") == "elevenlabs")):
            return []
        if (v.get("elevenlabs") or {}).get("default_voice"):
            return []
        recorded = set()
        if prov.get("tts") == "inbox":                    # hand-made recordings, in whatever format the inbox reads
            from .providers.inbox import AUDIO_EXTS
            from .shotpack import inbox_dir
            folder = inbox_dir(self.p.root, "dialogue")
            recorded = {f.stem for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXTS} if folder.is_dir() else set()
        carried = {b for path in self.p.shots.glob("*.json")
                   for shot in ShotList.model_validate_json(path.read_text(encoding="utf-8")).shots for b in shot.block_ids}
        chanted = set()                                   # chants go to voice.crowd_provider, not to ElevenLabs
        if v.get("crowd_provider") not in (None, "elevenlabs"):
            chanted = {bid for bid in (v.get("crowd") or {}) if crowd_voices(self.cfg, bid) > 1}
        cast, lock, missing = v.get("cast") or {}, self._lock(), []
        for path in sorted(self.p.scenes.glob("*.json")):
            for b in SceneScript.model_validate_json(path.read_text(encoding="utf-8")).blocks:
                cid = b.character_id
                if b.kind != "dialogue" or not cid or cid in missing or cast.get(cid) or b.block_id in recorded \
                        or b.block_id in chanted or (carried and b.block_id not in carried):
                    continue
                ref = getattr(lock.characters.get(cid), "voice_asset", None)
                try:
                    named = ref and getattr(self.library.resolve(ref), "elevenlabs_voice", None)
                except (KeyError, ValueError):
                    named = None
                if not named:
                    missing.append(cid)
        return missing

    def _identity_images(self):
        from .providers.google_ai import GeminiImages, google_client
        g = self.cfg.get("google", {}) or {}
        return GeminiImages(google_client(g, "image"), g, self.pricing, self.ledger)

    # ------------------------------------------------------------------ S6 shots

    def _shots(self) -> None:
        """One director call per scene. No call reads another scene's shot list, so they run at the same time
        (llm.concurrency); inputs are prepared and results cleaned, fitted, saved and logged here in scene order,
        so the files, reports and log are those of the one-at-a-time run."""
        bible = self._load(self.p.bible, StoryBible)
        outline = self._load(self.p.outline, Outline)
        lock = self._lock()
        prepared, prep_error = [], None
        for sc in outline.scenes:
            if (self.p.shots / f"{sc.scene_id}.json").exists():
                continue
            try:
                script = self._load(self.p.scenes / f"{sc.scene_id}.json", SceneScript)
                tags = [sc.set_piece] if sc.set_piece != "none" else []
                actions = [f"{h.asset.id}: {h.asset.description}" for h in
                           self.library.search("action", tags=tags, rasa=sc.rasa_primary, intensity=sc.intensity)]
                camera = self.navarasa["rasas"][sc.rasa_primary]["camera"]
            except Exception as e:          # the scenes before it are still planned, as in the sequential loop
                prep_error = e
                break
            prepared.append(SimpleNamespace(sc=sc, script=script, actions=actions, camera=camera))
        error = None
        lengths = self._grid_lengths()
        kw = {"clip_lengths": lengths} if lengths else {}
        from .characters import minors_off_screen
        off_screen = minors_off_screen(bible.characters, self.cfg)
        if off_screen:                      # the video model will not show them: planned out of frame from the start
            kw["off_screen"] = off_screen
            self.log(f"  kept out of frame (the video model shows no one under 18): {', '.join(off_screen.values())}")
        plans = {}
        for i, out in ordered_map(lambda p: self.director.shots(bible, lock, p.sc, p.script, p.actions, p.camera, **kw),
                                  prepared, workers(self.cfg), warm_first=True):
            if out.error is not None:
                error = error or out.error
                continue
            plans[i] = out.value
        if lengths:                         # plans the clip grid cannot take: asked once more, together; a failed ask
            off = [i for i, plan in plans.items()                         # keeps the first plan, fitted as before
                   if not shots_fit(len(plan.shots), prepared[i].sc.target_seconds, lengths)]

            def again(i):
                p, n = prepared[i], len(plans[i].shots)
                return self.director.shots(bible, lock, p.sc, p.script, p.actions, p.camera, **kw, issue=(
                    f"{n} shots, but a {p.sc.target_seconds} s scene takes between "
                    f"{least_shots(p.sc.target_seconds, lengths)} and {most_shots(p.sc.target_seconds, lengths)} clips "
                    f"of {min(lengths)} to {max(lengths)} s."))
            for j, out in ordered_map(again, off, workers(self.cfg)):
                if out.error is None:
                    plans[off[j]] = out.value
                else:
                    self._report("shot_issues", {"scene_id": prepared[off[j]].sc.scene_id, "billable_grid": (
                        f"asked again for a plan on the clip grid, which failed ({type(out.error).__name__}); "
                        "the first plan is fitted")})
        if off_screen:                      # a plan that still shows one: asked once more; a second miss is reported
            seen = {i: sorted({c for s in plan.shots for c in s.characters_in_frame if c in off_screen})
                    for i, plan in plans.items()}
            shown = [i for i, who in seen.items() if who]

            def without(i):
                p = prepared[i]
                return self.director.shots(bible, lock, p.sc, p.script, p.actions, p.camera, **kw, issue=(
                    f"it put {', '.join(off_screen[c] for c in seen[i])} in frame, and the video model will not "
                    "show them."))
            for j, out in ordered_map(without, shown, workers(self.cfg)):
                i = shown[j]
                if out.error is None and not any(c in off_screen for s in out.value.shots
                                                 for c in s.characters_in_frame):
                    plans[i] = out.value
                else:
                    self._report("shot_issues", {"scene_id": prepared[i].sc.scene_id, "off_screen": (
                        f"asked again to keep {', '.join(off_screen[c] for c in seen[i])} out of frame; the plan "
                        "still shows them, and those shots will stop the video step")})
        for i in sorted(plans):
            sc, script, plan = prepared[i].sc, prepared[i].script, plans[i]
            shots = self._clean_shots(plan.model_copy(update={"scene_id": sc.scene_id}), script, lock)
            shots = self._fit_shots(shots, sc)
            self._save(self.p.shots / f"{sc.scene_id}.json", shots)
            self.log(f"  {sc.scene_id}: {len(shots.shots)} shots")
        if error is not None:
            raise error
        if prep_error is not None:
            raise prep_error

    def _grid_lengths(self) -> list[int] | None:
        """The clip lengths the video model bills, when shots.billable_grid asks for shots on them; else None."""
        if not self._billable_grid():
            return None
        from .costing import veo_durations, veo_settings
        return veo_durations(self.pricing, veo_settings(self.cfg.get("google", {}))[2])

    def _billable_grid(self) -> bool:
        """shots.billable_grid: shot lengths on the video model's billed clip lengths (Veo 4/6/8 s), so no paid
        second is trimmed away. Off by default: existing projects keep their shot lists as fitted."""
        return bool((self.cfg.get("shots") or {}).get("billable_grid", False)) and \
            self.cfg.get("providers", {}).get("video") == "veo"

    def _fit_shots(self, shots: ShotList, sc) -> ShotList:
        fps = self._film["fps"]
        if self._billable_grid():
            from .costing import UnknownVideoTier, veo_durations, veo_settings
            g = self.cfg.get("google", {})

            def allowed(shot):
                try:
                    resolution = veo_settings(g, shot)[2]
                except UnknownVideoTier:
                    resolution = veo_settings(g)[2]
                return veo_durations(self.pricing, resolution)
            fitted = fit_shots_to_grid(shots, sc.target_seconds, allowed)
            if fitted is not None:
                return fitted
            self._report("shot_issues", {"scene_id": sc.scene_id, "billable_grid": (
                f"{sc.target_seconds} s cannot be met with {len(shots.shots)} shots on the billed lengths; "
                "fitted proportionally (some generated seconds will be trimmed)")})
        return fit_shots_to_scene(shots, sc.target_seconds, fps=fps)

    def _clean_shots(self, shots: ShotList, script: SceneScript, lock: CastLock) -> ShotList:
        cast = set(lock.characters)
        known_actions = {a.id for k, a in self.library.assets.items() if k.startswith("action:")}
        cleaned = []
        for shot in shots.shots:
            update = {}
            unknown = [c for c in shot.characters_in_frame if c not in cast]
            if unknown:
                self._report("shot_issues", {"shot_id": shot.shot_id, "dropped_characters": unknown})
                keep = [i for i, c in enumerate(shot.characters_in_frame) if c in cast]
                update["characters_in_frame"] = [shot.characters_in_frame[i] for i in keep]
                update["costume_ids"] = [shot.costume_ids[i] for i in keep if i < len(shot.costume_ids)]
            if shot.action_template_id and shot.action_template_id not in known_actions:
                update["action_template_id"] = None
            cleaned.append(shot.model_copy(update=update) if update else shot)
        covered = {b for s in cleaned for b in s.block_ids}
        for b in script.blocks:
            if b.block_id not in covered:
                self._report("shot_issues", {"scene_id": script.scene_id, "uncovered_block": b.block_id})
        return ShotList(scene_id=shots.scene_id, shots=cleaned)

    # ------------------------------------------------------------------ S12 music plan

    def _music_plan(self) -> None:
        path = self.p.music / "cue_sheet.json"
        if path.exists():
            return
        bible = self._load(self.p.bible, StoryBible)
        outline = self._load(self.p.outline, Outline)
        sheet = plan_cues(outline, bible, self.navarasa, self._lock())
        motifs = [f"{a.id}: raga {a.raga}, for {a.for_role}" for k, a in self.library.assets.items()
                  if k.startswith("motif:")]
        revisions = self.music_director.revise(bible, sheet, motifs)
        sheet, notes = apply_revisions(sheet, revisions.revisions, self.navarasa)
        for note in notes:
            self._report("music_notes", {"note": note})
        self._save(path, sheet)
        self.log(f"  {len(sheet.cues)} cues, {sum(c.peak for c in sheet.cues)} peaks")

    # ------------------------------------------------------------------ S8-S11 render

    def _render(self) -> None:
        timeline_path = self.p.edit / "timeline.json"
        only = list(self.cfg.get("render", {}).get("only") or [])
        crowd = dict(self.cfg.get("voice", {}).get("crowd") or {})
        if timeline_path.exists():
            previous = json.loads(timeline_path.read_text(encoding="utf-8"))
            if previous.get("only", []) == only and previous.get("takes", {}) == self.takes \
                    and previous.get("crowd", {}) == crowd:
                return
            self.log("  shot selection, chosen takes or crowd lines changed: re-rendering (paid clips are reused)")
            self.invalidate("render")
        outline = self._load(self.p.outline, Outline)
        lock = self._lock()
        prov = self.cfg["providers"]
        video = get_provider("video", prov["video"], **self._provider_kwargs("video", prov["video"], lock))
        if hasattr(video, "safer"):                     # a frame or clip Google refuses is worded again once
            video.safer, video.log = safer_shot_fn(self.p.root, lambda: self.director, self.log), self.log
            video.look_first = self.look_at_new_frames   # the runner's faces gate: a new still waits for a look
        self._to_look, self._slides = [], []
        tts = get_provider("tts", prov["tts"], **self._provider_kwargs("tts", prov["tts"], lock))
        music = get_provider("music", prov["music"], **self._provider_kwargs("music", prov["music"], lock))
        face_guard = self._face_guard(lock)
        voice_guard, speaker_embedder = self._voice_guard(lock)
        resolution = tuple(self.cfg.get("animatic", {}).get("resolution", [640, 360])) if self._animatic() \
            else tuple(self._film["resolution"])
        fps = self._film["fps"]

        uses_inbox = prov["video"] in ("veo", "shotpack")     # paid / hand-made clips live in inbox/video
        per_shot = {sid: o for sid, o in ((self.cfg.get("lipsync") or {}).get("shots") or {}).items() if isinstance(o, dict)}
        sync_len = {sid: float(o["sync_s"]) for sid, o in per_shot.items() if o.get("sync_s")}
        cut_in = {sid: float(o["cut_in_s"]) for sid, o in per_shot.items() if o.get("cut_in_s")}
        shots_out, dialogue_out, spans, speaker_of = [], [], [], {}
        takes_at = []                                 # each shot's take: (first frame on the film's clock, length)
        starts = scene_start_times(outline)
        film_end = sum(s.target_seconds for s in outline.scenes)
        prev_end = -DIALOGUE_GAP_S                            # end of the last placed line, across shots and scenes
        for sc in outline.scenes:
            script = self._load(self.p.scenes / f"{sc.scene_id}.json", SceneScript)
            blocks = {b.block_id: b for b in script.blocks}
            speaker_of.update({b.block_id: b.character_id for b in script.blocks if b.kind == "dialogue"})
            shot_list = self._load(self.p.shots / f"{sc.scene_id}.json", ShotList)
            ctx = RenderContext(cast=lock, scene_id=sc.scene_id, resolution=resolution, fps=fps)
            t, placed = starts[sc.scene_id], set()
            for shot in shot_list.shots:
                take = self.takes.get(shot.shot_id, 1)
                paid = clips_for(self.p.root, shot.shot_id) if uses_inbox else []
                # a lip-synced shot cut shorter in the edit keeps its take at the length it was lip-synced at
                # (lipsync.shots.<id>.sync_s), so that lip-sync is reused; the assembly plays its first duration_s.
                # One the edit cuts into later (cut_in_s) keeps the head it was lip-synced with, and the assembly
                # starts it that far in. A scene starts at a fixed time, so its first shot has no shot before it to
                # take that time.
                head = cut_in.get(shot.shot_id, 0.0)
                if head and shot is shot_list.shots[0]:
                    self._report("review_queue", {"shot_id": shot.shot_id, "reason": [
                        "lipsync cut_in_s ignored: a scene's first shot cannot start later (scene starts are fixed)"]})
                    head = 0.0
                origin, length = assemble.take_span(t, shot.duration_s, head, sync_len.get(shot.shot_id, 0.0))
                made = shot if length == shot.duration_s else shot.model_copy(update={"duration_s": length})
                if selected(shot, sc.scene_id, only):
                    clip = self._render_shot(video, made, ctx, face_guard)
                elif len(paid) >= take:                    # outside --only but already generated: use it, free
                    clip = ffmpeg.conform_clip(paid[take - 1], self.p.video / shot.shot_id / f"take_{take}.mp4",
                                               made.duration_s, ctx.resolution, ctx.fps)
                else:
                    clip = MockVideo().render_shot(shot, self.p.video / shot.shot_id / "placeholder.mp4", ctx,
                                                   note="not generated yet")
                real = not uses_inbox or bool(clips_for(self.p.root, shot.shot_id))
                shots_out.append(assemble.TimelineShot(shot.shot_id, sc.scene_id, round(t, 3), shot.duration_s,
                                                       clip, real, in_s=round(round(t, 3) - origin, 3)))
                spans.append((round(t, 3), shot))
                takes_at.append((origin, length))
                for bid in shot.block_ids:
                    block = blocks.get(bid)
                    if block is None or block.kind != "dialogue" or bid in placed:
                        continue
                    voice = lock.characters[block.character_id].voice_asset if block.character_id in lock.characters else None
                    wav, dur = self._render_line(tts, block, voice, voice_guard, speaker_embedder)
                    # Lines never overlap and are never cut: a long line runs on into the next shot or scene, and the
                    # next line waits for it. Only the film's last lines are pulled earlier to end with the film.
                    # the lead-in counts from the take's first frame: a shot cut into later keeps its lines in place
                    start = max(t - head + DIALOGUE_LEAD_IN_S, prev_end + DIALOGUE_GAP_S)
                    if start + dur > film_end:
                        start = max(prev_end + DIALOGUE_GAP_S, min(start, max(t - head, film_end - dur)))
                    start = round(start, 3)
                    dialogue_out.append(assemble.AudioEvent(wav, start, dur, "dialogue", ref_id=bid))
                    placed.add(bid)
                    prev_end = start + dur
                t += shot.duration_s

        self._hold_for_new_frames()
        self._stop_for_slides()

        lipsync = prov.get("lipsync") or "none"
        if lipsync != "none":                          # the speaker's mouth redrawn to the Telugu line, shot by shot
            syncer = get_provider("lipsync", lipsync, **self._provider_kwargs("lipsync", lipsync, lock))
            shots_out = self._lip_sync(syncer, shots_out, spans, takes_at, dialogue_out, speaker_of, resolution, fps)

        sheet = self._load(self.p.music / "cue_sheet.json", CueSheet)
        styles = load_styles(self.cfg)                 # music.styles: timed sections scored to the picture
        music_out = []
        for cue in sheet.cues:
            if not cue.instruments:
                continue   # deliberate silence
            cue = score_cue(cue, spans, styles)
            wav = music.render_cue(cue, self.p.music / f"{cue.cue_id}.wav")
            music_out.append(assemble.AudioEvent(wav, cue.start_s + cue.silence_before_s,
                                                 cue.duration_s - cue.silence_before_s, "music", -4.0, cue.cue_id))

        timeline = {
            "only": only,
            "takes": self.takes,
            "crowd": crowd,
            "film_seconds": sum(s.target_seconds for s in outline.scenes),
            "shots": [{**asdict(s), "clip": str(s.clip)} for s in shots_out],
            "dialogue": [{**asdict(e), "path": str(e.path)} for e in dialogue_out],
            "music": [{**asdict(e), "path": str(e.path)} for e in music_out],
        }
        self.p.edit.mkdir(parents=True, exist_ok=True)
        timeline_path.write_text(json.dumps(timeline, indent=1), encoding="utf-8")
        self.log(f"  rendered {len(shots_out)} shots, {len(dialogue_out)} lines, {len(music_out)} cues")

    def _lip_sync(self, syncer, shots_out: list, spans: list, takes: list, dialogue: list, speaker_of: dict[str, str],
                  resolution: tuple[int, int], fps: int) -> list:
        """Every generated shot that was filmed with a speaker in frame (the video model animates that mouth) gets the
        mouth redrawn to that speaker's own lines, as they fall inside the shot, and to silence everywhere else, so a
        face never mouths words without a voice. Off-screen voices never drive a face; placeholders are skipped, and
        so are shots set to `lipsync.shots.<shot_id>: {sync: false}`. Providers with apply_many get every shot at
        once (one GPU job loads the model once). With lipsync.speaker_lock (the default) LatentSync sees and changes
        only the speaker's mouth (_lip_sync_locked)."""
        ls = self.cfg.get("lipsync") or {}
        per_shot = ls.get("shots") or {}
        jobs = []                                       # (index, timeline shot, audio, raw output)
        for i, (ts, (_, shot), (origin, length)) in enumerate(zip(shots_out, spans, takes)):
            spoken = {b for b in shot.block_ids if speaker_of.get(b) in shot.characters_in_frame}
            opts = per_shot.get(shot.shot_id) or {}
            if not ts.real or not spoken or opts.get("sync") is False:
                continue
            # the take's own window on the film's clock (origin, length): longer than the shot, and starting before
            # it, when the edit cut it after lip-sync (sync_s, cut_in_s; see _render and take_span)
            events = [(e.path, e.start_s - origin, 0.0) for e in dialogue
                      if e.ref_id in spoken and e.start_s < origin + length and e.start_s + e.duration_s > origin]
            work = self.p.video / shot.shot_id
            audio = ffmpeg.place_audio(events, length, work / f"{ts.clip.stem}.lipsync.wav")
            windows = [(e.start_s - origin, e.start_s - origin + e.duration_s) for e in dialogue
                       if e.ref_id in spoken and e.start_s < origin + length and e.start_s + e.duration_s > origin]
            jobs.append((i, replace(ts, duration_s=length), audio, work / f"{ts.clip.stem}_lipsync_raw.mp4", windows))
        out = list(shots_out)
        if not jobs:
            return out
        if ls.get("speaker_lock", True):
            return self._lip_sync_locked(syncer, out, jobs, resolution, fps)
        try:
            if hasattr(syncer, "apply_many"):
                raws = syncer.apply_many([(ts.clip, audio, raw) for _, ts, audio, raw, _ in jobs])
            else:
                raws = [syncer.apply(ts.clip, audio, raw) for _, ts, audio, raw, _ in jobs]
        except (BudgetExceeded, GoogleAPIError) as stop:
            raise PipelineBlocked(str(stop)) from None
        kept = 0
        for (i, ts, audio, _, _), raw in zip(jobs, raws):
            if raw is None:                              # LatentSync could not do this shot: it keeps its clip
                why = syncer.failure(ts.clip, audio) if hasattr(syncer, "failure") else None
                self._report("review_queue", {"shot_id": ts.shot_id, "reason": [
                    "lip-sync failed; the clip is not lip-synced" + (f": {why.strip().splitlines()[-1]}" if why else "")]})
                kept += 1
                continue
            # back to the edit's exact length, size, frame rate and codec, so scenes still join by stream copy
            synced = ffmpeg.conform_clip(raw, raw.with_name(raw.name.replace("_raw", "")), ts.duration_s, resolution, fps)
            out[i] = replace(out[i], clip=synced)
        self.log(f"  lip-sync: {len(jobs) - kept} shots" + (f", {kept} kept unsynced (see review_queue)" if kept else ""))
        return out

    def _face_detector(self):
        if self.face_detector is None:
            self.face_detector = facelock.BlazeFace((self.cfg.get("lipsync") or {}).get("model_dir"))
        return self.face_detector

    def _face_mesh(self):
        if self.face_mesh is None:
            self.face_mesh = facelock.FaceMesh((self.cfg.get("lipsync") or {}).get("model_dir"))
        return self.face_mesh

    def _lip_sync_locked(self, syncer, out: list, jobs: list, resolution: tuple[int, int], fps: int) -> list:
        """Speaker lock (post/facelock.py). The speaker's face is found in every frame of the shot. A whole-frame
        result already paid for (the provider's cache) is reused when it really moved that face's mouth in all but
        lipsync.max_off_speaker_frames frames; otherwise LatentSync gets crops around the speaker with every other face
        blanked, one per run of frames where the speaker's face is visible. Either way the synced pixels go back only
        over the speaker's mouth and jaw. lipsync.offline: use only results made before, never start a GPU job.
        Every shot is reported to reports/lipsync.jsonl with a review sheet next to its clip (<take>.speaker.jpg).
        lipsync.sync_silence: `always` sends the speaker's whole visible run (silence included: the model closes a
        mouth the video model left moving); `auto` leaves silent stretches where the filmed mouth is already still
        as filmed (nothing to fix, nothing to pay); `never` syncs only the spoken windows and a margin."""
        ls = self.cfg.get("lipsync") or {}
        per_shot = ls.get("shots") or {}
        ratio, floor = float(ls.get("lock_ratio", 1.5)), float(ls.get("lock_floor", 10.0))
        max_off = int(ls.get("max_off_speaker_frames", 3))
        offline = bool(ls.get("offline", False)) and hasattr(syncer, "cached")
        detector = self._face_detector()
        plans: dict[int, tuple[facelock.Plan, dict]] = {}
        crops: list[tuple[int, tuple]] = []             # (index, (segment, crop video, crop audio, raw output))
        for i, ts, audio, raw, windows in jobs:
            sid, work, stem = ts.shot_id, raw.parent, ts.clip.stem
            opts = per_shot.get(sid) or {}
            analysis = facelock.analyse(ts.clip, detector, cache_dir=self.p.root / "cache" / "faces")
            track = facelock.pick_speaker(analysis, hint=opts.get("speaker"))
            facelock.review_sheet(ts.clip, analysis, track, work / f"{stem}.speaker.jpg")
            record: dict = {"shot_id": sid, "clip": str(ts.clip)}
            if track is None:
                self._report("review_queue", {"shot_id": sid, "reason": [
                    "lip-sync: the speaker's face was not found; the clip is not lip-synced"]})
                self._report("lipsync", {**record, "mode": "not synced", "why": "no speaker face found"})
                continue
            faces = facelock.fill_gaps(track, analysis.frames)
            keep = facelock.ranges(opts.get("keep_frames"))
            filmed = facelock.ranges(opts.get("filmed_frames"))     # as filmed in the paste-back only: no new crops
            silence = str(opts.get("sync_silence", ls.get("sync_silence", "always")))
            if silence in ("never", "auto"):                        # silent stretches left as filmed: fewer paid seconds
                skip = facelock.silent_frames(windows, analysis.frames, fps,
                                              margin=int(ls.get("silence_margin_frames", 6)))
                if silence == "auto":
                    skip = facelock.still_frames(faces, skip, set(range(analysis.frames)) - skip)
                skip -= keep | filmed
                if skip:
                    keep = keep | skip
                    record["silence_frames_as_filmed"] = len(skip)
                    opts = {"fade_frames": int(ls.get("silence_fade_frames", 3)), **opts}
            repair = [(int(a), int(b)) for a, b in opts.get("repair_frames") or []]
            bridge = int(opts.get("bridge_gaps") or 3)
            ext = opts.get("extend_track")                  # true, or the least overlap that counts as the same face
            carried = facelock.extend_track(track, analysis.tracks, 0.3 if ext is True else float(ext)) if ext else track
            shown = facelock.fill_gaps(carried, analysis.frames, max_gap=bridge) \
                if bridge > 3 or carried is not track else faces
            cx, cy = track.centre()
            record.update(speaker={"track": track.id, "at": [round(cx / analysis.width, 3), round(cy / analysis.height, 3)],
                                   "frames": sum(f is not None for f in faces), "of": analysis.frames,
                                   "hint": opts.get("speaker")})
            plan = facelock.Plan(shown, keep=keep | filmed, repair=repair, protect=opts.get("protect"),
                                 fade=int(opts.get("fade_frames") or 0))
            if opts.get("mesh", ls.get("mesh", False)):     # mask on the face mesh: on the lips, clipped to the face
                points = facelock.meshes(ts.clip, shown, self._face_mesh(), cache_dir=self.p.root / "cache" / "faces")
                plan.faces, plan.meshes = facelock.mesh_faces(shown, points)
                record["mesh_frames"] = sum(p is not None for p in plan.meshes)
            whole = syncer.cached(ts.clip, audio) if hasattr(syncer, "cached") and opts.get("mode") != "crop" else None
            if whole is not None:
                full = ffmpeg.conform_clip(whole, work / f"{stem}_lipsync_full.mp4", ts.duration_s, resolution, fps)
                off = facelock.off_speaker_frames(facelock.edit_locations(ts.clip, full, faces), keep, ratio, floor)
                record["whole_frame_missed_frames"] = off
                if len(off) <= max_off:
                    plan.full = full
                    plans[i] = (plan, {**record, "mode": "whole frame, speaker mask"})
                    continue
                plan.fallback = full                    # better than nothing if the crops cannot be made
                # frames only bridge_gaps / extend_track give a face have no crop: the whole-frame mouth goes there
                plan.fill = {n for n in range(analysis.frames)
                             if faces[n] is None and shown[n] is not None and n not in plan.keep}
            others: dict[int, list[list[float]]] = {}
            for t in analysis.tracks:
                if t.id != track.id:
                    for n, f in t.faces.items():
                        others.setdefault(n, []).append(f.box)
            segments = facelock.plan_segments(faces, analysis.width, analysis.height, keep)
            for k, seg in enumerate(segments):
                video = facelock.write_crop(ts.clip, seg, others, faces, work / f"{stem}_crop{k}.mp4")
                sound = facelock.cut_audio(audio, seg.start / fps, (seg.end - seg.start) / fps,
                                           work / f"{stem}_crop{k}.wav")
                crops.append((i, (seg, video, sound, work / f"{stem}_crop{k}_raw.mp4")))
            plans[i] = (plan, {**record, "mode": "speaker crops", "segments": [asdict(s) for s in segments]})
        todo = [(i, c) for i, c in crops if not offline or syncer.cached(c[1], c[2]) is not None]
        try:
            items = [(video, sound, raw) for _, (_, video, sound, raw) in todo]
            raws = (syncer.apply_many(items) if hasattr(syncer, "apply_many")
                    else [syncer.apply(*item) for item in items]) if items else []
        except (BudgetExceeded, GoogleAPIError) as stop:
            raise PipelineBlocked(str(stop)) from None
        done = {id(c): r for (_, c), r in zip(todo, raws)}
        for i, c in crops:
            seg, video, sound, raw_out = c
            plan, record = plans[i]
            got = done.get(id(c))
            if got is None:
                why = ("not made yet (lipsync.offline)" if id(c) not in {id(x) for _, x in todo} else
                       (syncer.failure(video, sound) or "failed") if hasattr(syncer, "failure") else "failed")
                record.setdefault("failed_segments", []).append({"start": seg.start, "end": seg.end,
                                                                 "why": why.strip().splitlines()[-1][:300]})
                continue
            synced = ffmpeg.conform_clip(got, raw_out.with_name(raw_out.name.replace("_raw", "_synced")),
                                         (seg.end - seg.start) / fps, seg.window[2:], fps)
            plan.segments.append((seg, synced))
        made = kept = 0
        for i, (plan, record) in sorted(plans.items()):
            ts = out[i]
            if plan.full is None and not plan.segments and plan.fallback is not None:
                plan.full = plan.fallback               # synced where it hit the speaker; the wrong faces never show
                record["mode"] = "whole frame, speaker mask (crops not made)"
                self._report("review_queue", {"shot_id": ts.shot_id, "reason": [
                    f"lip-sync: speaker crops not made; the whole-frame result is used through the speaker mask, "
                    f"unsynced in {len(record['whole_frame_missed_frames'])} frames"]})
            if plan.full is None and not plan.segments:
                kept += 1
                self._report("review_queue", {"shot_id": ts.shot_id, "reason": [
                    "lip-sync: no synced frames; the clip is not lip-synced"
                    + (f" ({record['failed_segments'][-1]['why']})" if record.get("failed_segments") else "")]})
                self._report("lipsync", {**record, "mode": "not synced"})
                continue
            detail = (per_shot.get(ts.shot_id) or {}).get("detail", ls.get("detail", 1.0))
            result = facelock.composite(ts.clip, plan, ts.clip.with_name(f"{ts.clip.stem}_lipsync.mp4"),
                                        detail=float(detail))
            covered = range(len(plan.faces)) if plan.full is not None else \
                [n for s, _ in plan.segments for n in range(s.start, s.end)]
            record["synced_frames"] = sum(1 for n in covered if n not in plan.keep and plan.faces[n] is not None)
            self._report("lipsync", record)
            out[i] = replace(ts, clip=result)
            made += 1
        self.log(f"  lip-sync (speaker lock): {made} shots"
                 + (f", {kept} kept unsynced (see review_queue)" if kept else "")
                 + (f", {sum(1 for _, c in crops if id(c) in done)} speaker crops" if crops else ""))
        return out

    def _hold_for_new_frames(self) -> None:
        """Stills drawn while the video was made (missing at the faces gate, or redrawn after Google refused the
        shot) are not animated until the owner has looked: the faces approval is taken back and faces held again,
        naming them. Every clip already made is kept, so the next run pays only for these."""
        if not self._to_look:
            return
        (self.p.reviews / "faces.approved").unlink(missing_ok=True)
        self.p.reviews.mkdir(parents=True, exist_ok=True)
        (self.p.reviews / "faces.pending").write_text(
            f"New first frames were drawn while the video was made: {', '.join(self._to_look)} (missing, or drawn "
            "again after Google refused the shot). Look at them in the control room, then approve faces; every clip "
            "already made is kept.\n", encoding="utf-8")
        raise GatePending("faces", self.p.root)

    def _stop_for_slides(self) -> None:
        """Shots Google refused twice have no video. A film is never cut with a text slide in their place: the video
        step stops here, before lip-sync, music and the cut are paid, and names them (the film page's Send back
        replans them from the owner's notes). Every clip already made is kept."""
        if not self._slides:
            return
        raise PipelineBlocked(
            f"{len(self._slides)} shot(s) have no video, Google's safety filter refused them: "
            f"{', '.join(self._slides)}. "
            "Send them back for rework with a note (who or what should be in frame); every clip made is kept")

    def _render_shot(self, video, shot, ctx: RenderContext, face_guard) -> Path:
        # Without a face guard you choose the take (takes.yaml, `nraiorbit retake`); with one, it retakes itself.
        first = self.takes.get(shot.shot_id, 1)          # a Send back moves a shot on to a fresh take
        last = first + self.cfg["retakes"]["max_per_shot"] - 1 if face_guard else first
        clip, last_result = None, None
        for take in range(first, last + 1):
            clip = self.p.video / shot.shot_id / f"take_{take}.mp4"
            if not clip.exists():
                try:
                    video.render_shot(shot, clip, replace(ctx, seed=take))
                except ShotFiltered as blocked:            # no text slide goes into the film: named after the loop
                    self._report("review_queue", {"shot_id": shot.shot_id, "reason": [str(blocked)]})
                    self.log(f"  {shot.shot_id}: refused by Google's safety filter (reworded once where it could "
                             "be); no video for it")
                    self._slides.append(shot.shot_id)
                    return clip
                except (BudgetExceeded, GoogleAPIError) as stop:
                    raise PipelineBlocked(str(stop)) from None
                except FileNotFoundError as missing:
                    raise PipelineBlocked(str(missing)) from None
                except FirstFrameToLookAt:                 # drawn, not animated: the faces gate comes after the loop
                    self._to_look.append(shot.shot_id)
                    return clip
            if face_guard is None or not shot.characters_in_frame:
                return clip
            last_result = self._check_identity(face_guard, shot, clip)
            self._report("guards", {"guard": "identity", "shot_id": shot.shot_id, "take": take, **last_result.to_dict()})
            if last_result.passed:
                return clip
        self._report("review_queue", {"shot_id": shot.shot_id, "reason": last_result.issues if last_result else []})
        return clip

    def _crowd_voices(self, block_id: str) -> int:
        """voice.crowd.<block_id>.voices in pipeline.yaml: that line is chanted by this many voices (1: a single voice)."""
        return crowd_voices(self.cfg, block_id)

    def _render_line(self, tts, block, voice, voice_guard, speaker_embedder) -> tuple[Path, float]:
        crowd = self._crowd_voices(block.block_id)
        chants = tts.can_chant(block) if hasattr(tts, "can_chant") else hasattr(tts, "synthesize_crowd")
        if crowd > 1 and not chants:
            self.log(f"  {block.block_id}: the tts provider cannot voice a crowd; using the single voice")
            crowd = 1
        if crowd > 1:
            voice_guard = None             # a chant of several voices never matches one speaker's centroid
        takes = self.cfg["voice"]["takes_per_line"] if voice_guard else 1
        results, files = [], {}
        for take in range(1, takes + 1):
            prefix = f"crowd{crowd}_" if crowd > 1 else ""     # a chant never reuses (or replaces) a single take
            wav = self.p.dialogue / block.block_id / f"{prefix}take_{take}.wav"
            try:
                if wav.exists():
                    dur = ffmpeg.duration(wav)
                elif crowd > 1:
                    dur = tts.synthesize_crowd(block, voice, wav, crowd)
                else:
                    dur = tts.synthesize(block, voice, wav)
            except ShotFiltered as blocked:
                self._report("review_queue", {"line_id": block.block_id, "reason": [str(blocked)]})
                dur = MockTTS().synthesize(block, voice, wav)
            except (BudgetExceeded, GoogleAPIError) as stop:
                raise PipelineBlocked(str(stop)) from None
            files[f"take_{take}"] = (wav, dur)
            if voice_guard is None:
                return wav, dur
            measured = VoiceTake(f"take_{take}", speaker_embedding=speaker_embedder.embed(wav), duration_s=dur)
            result = voice_guard.check(measured, voice_id=voice, script_te=block.text_te,
                                       target_rasa=block.delivery.rasa if block.delivery else "shanta")
            self._report("guards", {"guard": "voice", "line_id": block.block_id, **asdict(result)})
            results.append(result)
        best = voice_guard.select_best(results)
        if best is None:
            self._report("review_queue", {"line_id": block.block_id, "reason": [r.issues for r in results]})
            best = max(results, key=lambda r: r.score)
        return files[best.take_id]

    def _provider_kwargs(self, capability: str, name: str, lock: CastLock) -> dict:
        """Constructor arguments for providers that need project context."""
        voices = {e.voice_asset: self.library.resolve(e.voice_asset).model_dump()
                  for e in lock.characters.values() if e.voice_asset}
        if capability == "lipsync":
            return {} if name == "mock" else {"project_root": self.p.root, "cfg": self.cfg, "pricing": self.pricing,
                                              "ledger": self.ledger}
        if name in ("shotpack", "inbox") and capability in ("video", "music"):
            return {"project_root": self.p.root}
        if capability == "video" and name == "veo":
            return {"project_root": self.p.root, "library": self.library, "cfg": self.cfg, "pricing": self.pricing,
                    "ledger": self.ledger, "navarasa": self.navarasa}
        if capability == "music" and name == "lyria":
            return {"project_root": self.p.root, "cfg": self.cfg, "pricing": self.pricing, "ledger": self.ledger,
                    "navarasa": self.navarasa}
        if capability != "tts":
            return {}
        if name == "mock":
            return {"pitch_by_voice": {ref: v.get("pitch") or "mid" for ref, v in voices.items()}}
        if name == "indic_parler":
            return {"voices": voices}
        if name == "gemini":
            return {"voices": voices, "cfg": self.cfg, "pricing": self.pricing, "ledger": self.ledger}
        if name == "elevenlabs":                        # chants (voice.crowd) go to voice.crowd_provider
            crowd_name = self.cfg.get("voice", {}).get("crowd_provider")
            crowd = get_provider("tts", crowd_name, **self._provider_kwargs("tts", crowd_name, lock)) if crowd_name else None
            return {"voices": voices, "cfg": self.cfg, "pricing": self.pricing, "ledger": self.ledger, "crowd": crowd}
        if name == "inbox":
            fallback = self.cfg.get("voice", {}).get("inbox_fallback", "mock")
            fallback = "mock" if fallback == "inbox" else fallback
            return {"project_root": self.p.root, "fallback": fallback,
                    **self._provider_kwargs("tts", fallback, lock)}
        return {}

    def _centroid(self, ref: str, key: str) -> np.ndarray:
        asset = self.library.resolve(ref)
        if key not in asset.files:
            raise PipelineBlocked(f"{ref} has no '{key}' in its manifest files; compute it from the identity pack")
        return np.load(self.library.file_path(asset, key))

    def _face_guard(self, lock: CastLock):
        name = self.cfg["providers"].get("face_embedder", "none")
        if name in (None, "none"):
            self.log("  identity guard: off (providers.face_embedder is 'none')")
            return None
        centroids = {cid: self._centroid(e.character_asset, "face_centroid") for cid, e in lock.characters.items()}
        return IdentityGuard(centroids), get_provider("face_embedder", name)

    def _check_identity(self, face_guard, shot, clip: Path):
        guard, embedder = face_guard
        gcfg = self.cfg["guards"]["identity"]
        frames_dir = clip.with_suffix("") / "frames"
        frames = ffmpeg.extract_frames(clip, gcfg["sample_every_n_frames"], frames_dir)
        observations = [embedder.detect_and_embed(f) for f in frames]
        overrides = {}
        for cid in shot.characters_in_frame:   # strictest per-character override wins
            asset = self.library.resolve(self._lock().characters[cid].character_asset)
            for k, v in asset.guard.items():
                overrides[k] = max(v, overrides.get(k, v))
        thresholds = IdentityThresholds.for_shot(gcfg, shot.size, overrides)
        return guard.check_clip(observations, shot.characters_in_frame, thresholds,
                                faces_required=shot.size not in ("EWS", "WS", "aerial"))

    def _voice_guard(self, lock: CastLock):
        name = self.cfg["providers"].get("speaker_embedder", "none")
        if name in (None, "none"):
            self.log("  voice guard: off (providers.speaker_embedder is 'none')")
            return None, None
        centroids = {e.voice_asset: self._centroid(e.voice_asset, "speaker_centroid")
                     for e in lock.characters.values() if e.voice_asset}
        return VoiceGuard(centroids, VoiceThresholds.from_config(self.cfg["guards"]["voice"])), \
            get_provider("speaker_embedder", name)

    # ------------------------------------------------------------------ S14 assemble + master

    def _assemble(self) -> None:
        movie = self.p.master / "movie.mp4"
        film = self.p.edit / "film_dialogue.mp4"             # the cut before music: what the trailer is cut from
        if not movie.exists() and not (self.p.edit / "movie_nocards.mp4").exists():
            self._master(movie)
        elif not film.exists():
            # a fresh container restored the master but not 10_edit (never backed up): master again from the
            # timeline the render step just re-made from the caches, so the trailer has its source; nothing paid
            self.log("  assemble: 10_edit/film_dialogue.mp4 is missing; re-mastering from the timeline")
            movie.unlink(missing_ok=True)
            (self.p.edit / "movie_nocards.mp4").unlink(missing_ok=True)   # never re-card an old clean copy
            self._master(movie)
        self._place_cards(movie)
        self._write_edl()
        self.gate("pacing" if self._animatic() else "final_cut")

    def _write_edl(self) -> None:
        """Where every shot and line sits in the cut, in reports/ (backed up; 10_edit is not): what the editor reads
        when the owner rejects the cut or the trailer, after a fresh container has no timeline yet."""
        path = self.p.edit / "timeline.json"
        if not path.exists():
            return
        tl = json.loads(path.read_text(encoding="utf-8"))
        edl = {"film_seconds": tl.get("film_seconds"),
               "shots": [{"shot_id": s["shot_id"], "scene_id": s.get("scene_id"), "start_s": s["start_s"],
                          "duration_s": s["duration_s"], "real": s.get("real", True)} for s in tl["shots"]],
               "dialogue": [{"ref_id": e["ref_id"], "start_s": e["start_s"], "duration_s": e["duration_s"]}
                            for e in tl["dialogue"]]}
        self.p.reports.mkdir(parents=True, exist_ok=True)
        (self.p.reports / EDL).write_text(json.dumps(edl, indent=1), encoding="utf-8")

    def _owner_mix(self) -> tuple[float, float]:
        """The owner's change to the mix (reports/owner_mix.json, from the editor): dB added to the music, and the
        least a line must sit above the music (0 = none asked; the project's audio.min_voice_over_music_db holds
        whenever it is higher)."""
        try:
            mix = json.loads((self.p.reports / OWNER_MIX).read_text(encoding="utf-8"))
            return float(mix.get("music_gain_db") or 0.0), float(mix.get("min_voice_over_music_db") or 0.0)
        except (OSError, ValueError, TypeError, AttributeError):
            return 0.0, 0.0

    def _trailer_direction(self) -> TrailerDirection | None:
        """The owner's wishes for the trailer (reports/trailer_direction.json, from the editor), if any."""
        try:
            data = json.loads((self.p.reports / TRAILER_DIRECTION).read_text(encoding="utf-8"))
            return TrailerDirection.model_validate(data["direction"]) if data.get("direction") else None
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _master(self, movie: Path) -> None:
        """Assemble the scenes and master the film into `movie`, without cards."""
        (self.p.edit / "cards.json").unlink(missing_ok=True)       # a fresh master carries no cards
        tl = json.loads((self.p.edit / "timeline.json").read_text(encoding="utf-8"))
        outline = self._load(self.p.outline, Outline)
        starts = scene_start_times(outline)
        shots = [assemble.TimelineShot(**{**s, "clip": Path(s["clip"])}) for s in tl["shots"]]
        dialogue = self._levelled([assemble.AudioEvent(**{**e, "path": Path(e["path"])}) for e in tl["dialogue"]])
        louder = self._owner_mix()[0]
        music = [assemble.AudioEvent(**{**e, "path": Path(e["path"]), "gain_db": e.get("gain_db", 0.0) + louder})
                 for e in tl["music"]]
        scene_files = []
        for sc in outline.scenes:
            scene_shots = [s for s in shots if s.scene_id == sc.scene_id]
            start, end = starts[sc.scene_id], starts[sc.scene_id] + sc.target_seconds
            # every line overlapping the scene: one that runs past a cut is split there and joined seamlessly
            scene_dlg = [e for e in dialogue if e.start_s < end and e.start_s + e.duration_s > start]
            out = self.p.edit / "scenes" / f"{sc.scene_id}.mp4"
            scene_files.append(assemble.render_scene(scene_shots, scene_dlg, starts[sc.scene_id], out,
                                                     fps=self._film["fps"]))
        film = assemble.join_scenes(scene_files, self.p.edit / "film_dialogue.mp4")
        audio = self.cfg["audio"]
        assemble.master(film, music, movie, audio["master_lufs"], audio["true_peak_db"])
        self._clarify(movie, dialogue, "film")
        self._write_stems(film, movie)
        self._write_subtitles(dialogue)
        self.log(f"  master: {movie} ({ffmpeg.duration(movie) / 60:.1f} min)")

    def _levelled(self, lines: list) -> list:
        """Every dialogue take brought to the same speaking level (audio.dialogue_level_dbfs), so a quiet take is
        not lost under the music and a loud one does not jump out."""
        target = self.cfg["audio"].get("dialogue_level_dbfs")
        if target is None:
            return lines
        return [replace(e, gain_db=e.gain_db + level_gain_db(e.path, target)) for e in lines]

    def _clarify(self, master: Path, lines: list, label: str) -> None:
        """Lift any line that is not audio.min_voice_over_music_db above the music in the finished mix (the music
        under it stays as it is), then put the new sound back under the untouched picture."""
        audio = self.cfg["audio"]
        min_db = max(self._owner_mix()[1], audio.get("min_voice_over_music_db") or 0)   # the owner only raises it
        if not min_db or not lines:
            return
        work = master.parent / f".{master.stem}" / "clarity"
        wav = work / "mix.wav"
        report = clarify_dialogue(master, [(e.path, e.start_s, e.gain_db) for e in lines], wav, min_db=min_db,
                                  true_peak=audio["true_peak_db"])
        lifted = [r for r in report if r["boost_db"] > 0]
        self._report("mix", {"master": label, "min_voice_over_music_db": min_db, "lines": report})
        if lifted:
            tmp = work / f"remux{master.suffix}"
            ffmpeg.mux(master, wav, tmp, bitrate="256k")       # every frame kept: the remux once cut the last 0.2 s
            tmp.replace(master)
        self.log(f"  dialogue clarity ({label}): {len(lifted)} of {len(report)} lines lifted"
                 + (f", at most +{max(r['boost_db'] for r in lifted):.1f} dB" if lifted else ""))

    def _place_cards(self, movie: Path) -> None:
        """Lay the configured cards (title, INTERVAL, end) over the master; changing them later never re-masters.

        While cards are on, the master without them is kept in 10_edit/movie_nocards.mp4 and cards.json records the
        cards on movie.mp4. Without cards movie.mp4 is the master itself, exactly as assembled.
        """
        cards = [Card.from_config(c, self.p.root) for c in self.cfg.get("cards") or []]
        wanted = [c.record() for c in cards]
        clean, record = self.p.edit / "movie_nocards.mp4", self.p.edit / "cards.json"
        if not clean.exists() and record.exists():
            # cards are on movie.mp4 but its clean copy is gone (10_edit cleared): master it again, never card twice
            self.log("  cards: the master without cards (10_edit/movie_nocards.mp4) is missing; re-mastering")
            movie.unlink(missing_ok=True)
            self._master(movie)
        if not clean.exists():
            applied = []                          # movie.mp4 is the clean master
        elif record.exists() and movie.exists():
            applied = json.loads(record.read_text(encoding="utf-8"))
        else:
            applied = None                        # an interrupted card pass: redo it from the clean master
        if wanted == applied:
            return
        if clean.exists():
            movie.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
        else:
            clean.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(movie, clean)
        if not cards:
            shutil.move(clean, movie)
            record.unlink(missing_ok=True)
            self.log("  cards: none")
            return
        resolution = tuple(self.cfg.get("animatic", {}).get("resolution", [640, 360])) if self._animatic() \
            else tuple(self._film["resolution"])
        apply_cards(clean, cards, movie, resolution, self._film["fps"], card_dir=self.p.edit / "cards", log=self.log)
        record.write_text(json.dumps(wanted, indent=1, ensure_ascii=False), encoding="utf-8")
        self.log(f"  cards: {', '.join(f'{c.kind} at {c.at_s:.1f} s' for c in cards)}")

    def _write_stems(self, film_with_dialogue: Path, movie: Path) -> None:
        """Separate dialogue and music tracks for mixing in DaVinci Resolve (Fairlight)."""
        stems = self.p.master / "stems"
        stems.mkdir(parents=True, exist_ok=True)
        joined = film_with_dialogue.parent / f".{film_with_dialogue.stem}" / "dialogue.wav"   # lossless scene join
        source = joined if joined.exists() else film_with_dialogue
        ffmpeg.run(["-i", str(source), "-vn", "-c:a", "pcm_s24le", str(stems / "dialogue.wav")])
        music_bed = movie.parent / f".{movie.stem}" / "music.wav"
        if music_bed.exists():
            shutil.copy2(music_bed, stems / "music.wav")

    def _script_blocks(self) -> dict:
        return {b.block_id: b for path in sorted(self.p.scenes.glob("*.json")) for b in self._load(path, SceneScript).blocks}

    def _write_subtitles(self, dialogue: list[assemble.AudioEvent]) -> None:
        blocks = self._script_blocks()
        for lang in self._film.get("subtitle_languages", ["te", "en"]):
            entries = [(e.start_s, e.start_s + e.duration_s,
                        getattr(blocks[e.ref_id], f"text_{lang}")) for e in dialogue if e.ref_id in blocks]
            write_srt(entries, self.p.master / f"subs_{lang}.srt")

    # ------------------------------------------------------------------ S15 trailer

    def _trailer(self) -> None:
        out = self.p.master / "trailer.mp4"
        if not out.exists():
            bible = self._load(self.p.bible, StoryBible)
            outline = self._load(self.p.outline, Outline)
            tl = json.loads((self.p.edit / "timeline.json").read_text(encoding="utf-8"))
            starts = {s["shot_id"]: s["start_s"] for s in tl["shots"]}
            real = {s["shot_id"] for s in tl["shots"] if s.get("real", True)}
            all_shots = []
            for sc in outline.scenes:
                for shot in self._load(self.p.shots / f"{sc.scene_id}.json", ShotList).shots:
                    if shot.shot_id in real:                     # never cut placeholders into the trailer
                        all_shots.append((shot, starts[shot.shot_id]))
            blocks = self._script_blocks()
            lines = [TrailerLine(e["ref_id"], e["start_s"], e["duration_s"], b.character_id,
                                 b.delivery.rasa if b.delivery else "shanta", b.delivery.intensity if b.delivery else 5)
                     for e in tl["dialogue"] if (b := blocks.get(e["ref_id"]))]
            fps = self._film["fps"]
            direction = self._trailer_direction()
            target = self._film["trailer_seconds"]
            if direction is not None and direction.target_seconds > 0:     # the owner's length, within reason
                target = max(20.0, min(1.5 * target, direction.target_seconds))
            plan = plan_trailer(all_shots, bible, tl["film_seconds"], target, lines,
                                max_line_s=self._film.get("trailer_line_max_s", 7.5),
                                max_lines=self._film.get("trailer_max_lines", 5), fps=fps, direction=direction)
            planned = json.dumps([asdict(c) for c in plan], indent=1)
            (self.p.edit / "trailer_plan.json").write_text(planned, encoding="utf-8")
            self.p.reports.mkdir(parents=True, exist_ok=True)
            (self.p.reports / TRAILER_PLAN).write_text(planned, encoding="utf-8")
            if direction is not None:                    # what the cut could not honour, for the owner to see
                record = json.loads((self.p.reports / TRAILER_DIRECTION).read_text(encoding="utf-8"))
                record["unmet"] = unmet(plan, all_shots, tl["film_seconds"], direction)
                (self.p.reports / TRAILER_DIRECTION).write_text(json.dumps(record, indent=1, ensure_ascii=False),
                                                                encoding="utf-8")
                for miss in record["unmet"]:
                    self.log(f"  trailer: the owner's direction not met: {miss}")
            resolution = tuple(self.cfg.get("animatic", {}).get("resolution", [640, 360])) if self._animatic() \
                else tuple(self._film["resolution"])
            # Picture only: the film's sound would chop lines at every cut. Whole lines are laid over it instead.
            title_image = next((p for p in (self.p.root / "inbox" / f"title{e}" for e in (".png", ".jpg", ".jpeg"))
                                if p.exists()), None)       # a designed title card, if there is one
            title_at = next((i for i, c in enumerate(plan) if c.shot_id is None), 0)
            behind = next((c for c in reversed(plan[:title_at]) if c.shot_id), None)   # the punch shot, blurred
            raw = assemble.cut_segments(
                self.p.edit / "film_dialogue.mp4",
                [(None if c.shot_id is None else c.film_start_s, c.duration_s) for c in plan],
                self.p.edit / "trailer_raw.mp4", title=bible.title_en, resolution=resolution, fps=fps, audio=False,
                title_image=title_image,
                title_backdrop_s=behind.film_start_s + behind.duration_s / 2 if behind else None)
            total = round(sum(c.duration_s for c in plan), 4)
            wavs = {e["ref_id"]: Path(e["path"]) for e in tl["dialogue"]}
            said = self._levelled([assemble.AudioEvent(wavs[c.line_id], c.line_at_s, 0.0, "dialogue", ref_id=c.line_id)
                                   for c in plan if c.line_id])
            dialogue = ffmpeg.place_audio([(e.path, e.start_s, e.gain_db) for e in said], total,
                                          self.p.edit / ".trailer_dialogue" / "dialogue.wav")
            film = ffmpeg.mux(raw, dialogue, self.p.edit / "trailer_dialogue.mp4")
            notes, drop_at = elevation_arc(plan)
            if direction is not None and direction.music_notes.strip():
                notes += f" The owner asks: {direction.music_notes.strip()}"
            cue = Cue(cue_id="trailer", scene_ids=[], start_s=0, duration_s=total, rasa="veera", intensity=9,
                      cue_type="trailer", raga="nattai", tala="adi", bpm=140,
                      instruments=["venu_flute", "veena", "thavil", "dappu", "epic_brass", "choir_sanskrit_chant"],
                      leitmotif_character=None, peak=True, notes=notes)
            self._write_trailer_cue(cue)
            name = self.cfg["providers"]["music"]
            music = get_provider("music", name, **self._provider_kwargs("music", name, self._lock()))
            wav = music.render_cue(cue, self.p.music / "trailer_cue.wav")
            audio = self.cfg["audio"]
            # the score drops to near-silence right before the punch line, then hits on the title card
            score = assemble.AudioEvent(wav, 0.0, total, "music", -2.0 + self._owner_mix()[0], "trailer")
            assemble.master(film, [score], out, audio["master_lufs"], audio["true_peak_db"],
                            music_dips=[(drop_at, PUNCH_DROP_S, -20.0)])
            self._clarify(out, said, "trailer")
            self.log(f"  trailer: {out} ({total:.0f} s, {sum(c.shot_id is not None for c in plan)} clips, "
                     f"{sum(bool(c.line_id) for c in plan)} whole lines)")
        self.gate("trailer")

    def _write_trailer_cue(self, cue: Cue) -> None:
        """The trailer cue with its timed elevation arc, for review and for re-scoring by hand."""
        self._save(self.p.music / "trailer_cue.json", cue)
