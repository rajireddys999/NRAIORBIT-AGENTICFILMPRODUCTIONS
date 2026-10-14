"""Stage orchestrator: story -> screenplay -> cast -> shots -> music -> render -> edit -> trailer.

Every stage writes its artifact into the project directory and is skipped on re-run if
the artifact exists, so a crash or a review gate never loses work. `force` invalidates
a stage and everything downstream of it. In production the same stages run as Temporal
activities (docs/INFRA_COST_ROADMAP.md); the contracts do not change.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

import numpy as np
import yaml
from pydantic import BaseModel

from .agents import CastingAgent, DialogueCritic, Director, MusicDirector, Screenwriter, StoryAnalyst
from .config import REPO_ROOT, load_navarasa, load_pipeline_config
from .costing import BudgetExceeded, SpendLedger, load_pricing, load_takes
from .guards.identity import IdentityGuard, IdentityThresholds
from .guards.voice import VoiceGuard, VoiceTake, VoiceThresholds
from .library.registry import AssetRegistry
from .llm import LLM
from .planning.music import apply_revisions, plan_cues
from .planning.score import load_styles, score_cue
from .planning.runtime import (
    TEMPLATES, describe_template, fit_shots_to_scene, rebalance_outline, scene_start_times, validate_outline,
)
from .post import assemble, ffmpeg
from .post.subtitles import write_srt
from .post.trailer import PUNCH_DROP_S, TrailerLine, elevation_arc, plan_trailer
from .providers import RenderContext, get_provider
from .providers.google_ai import GoogleAPIError, ShotFiltered
from .providers.mock import MockTTS, MockVideo
from .shotpack import clips_for, selected
from .schemas import CastLock, Critique, Cue, CueSheet, Outline, SceneScript, ShotList, StoryBible

STAGES = ["story_bible", "outline", "scenes", "cast", "shots", "music_plan", "render", "assemble", "trailer"]
STAGE_GATES = {"story_bible": "story", "cast": "cast", "assemble": "pacing", "trailer": "trailer"}
DIALOGUE_LEAD_IN_S = 0.2
DIALOGUE_GAP_S = 0.25        # breath between consecutive lines


class GatePending(Exception):
    def __init__(self, gate: str, project_dir: Path):
        self.gate = gate
        super().__init__(f"review gate '{gate}' is waiting for approval in {project_dir}")


class PipelineBlocked(Exception):
    pass


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
                         self.master / "stems"],
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
        self.takes = load_takes(self.p.root)
        self.log = log
        self.story_analyst = StoryAnalyst(llm, self.cfg)
        self.screenwriter = Screenwriter(llm, self.cfg)
        self.critic = DialogueCritic(llm, self.cfg)
        self.casting = CastingAgent(llm, self.cfg)
        self.director = Director(llm, self.cfg)
        self.music_director = MusicDirector(llm, self.cfg)

    # ------------------------------------------------------------------ driver

    def run(self, until: str | None = None, force: tuple[str, ...] = ()) -> None:
        if force:
            self.invalidate(min(force, key=STAGES.index))
        for stage in STAGES:
            self.log(f"[{stage}]")
            getattr(self, f"_{stage}")()
            if stage == until:
                return

    def invalidate(self, stage: str) -> None:
        for s in STAGES[STAGES.index(stage):]:
            for path in self.p.stage_outputs(s):
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            gate = STAGE_GATES.get(s)
            if gate:
                for suffix in ("approved", "pending"):
                    (self.p.reviews / f"{gate}.{suffix}").unlink(missing_ok=True)

    def status(self) -> dict[str, str]:
        out = {}
        for s in STAGES:
            done = all(p.exists() for p in self.p.stage_outputs(s)[:1])
            gate = STAGE_GATES.get(s)
            if gate and (self.p.reviews / f"{gate}.pending").exists():
                out[s] = f"waiting for review '{gate}'"
            else:
                out[s] = "done" if done else "todo"
        return out

    def gate(self, name: str) -> None:
        g = self.cfg.get("review_gates", {}).get(name, {})
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

        outline = rebalance_outline(self.screenwriter.outline(bible, runtime, guide), runtime)
        issues = check(outline)
        if issues:
            self.log(f"  outline issues, asking for a revision: {issues}")
            outline = rebalance_outline(self.screenwriter.outline(bible, runtime, guide, issues=issues), runtime)
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
            script = script.model_copy(update={"scene_id": sc.scene_id})
            self._save(path, script)
            previous = script.synopsis
            self.log(f"  {sc.scene_id}: {sum(b.kind == 'dialogue' for b in script.blocks)} lines")

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
        self.gate("cast")
        pending = [cid for cid, e in self._lock().characters.items() if e.character_asset.endswith("@pending")]
        if pending and not self._animatic():
            raise PipelineBlocked(
                f"characters {pending} need identity packs (briefs in {self.p.briefs}). "
                "Build the packs, register them in library/, then re-run with --force cast.")

    # ------------------------------------------------------------------ S6 shots

    def _shots(self) -> None:
        bible = self._load(self.p.bible, StoryBible)
        outline = self._load(self.p.outline, Outline)
        lock = self._lock()
        for sc in outline.scenes:
            path = self.p.shots / f"{sc.scene_id}.json"
            if path.exists():
                continue
            script = self._load(self.p.scenes / f"{sc.scene_id}.json", SceneScript)
            tags = [sc.set_piece] if sc.set_piece != "none" else []
            actions = [f"{h.asset.id}: {h.asset.description}" for h in
                       self.library.search("action", tags=tags, rasa=sc.rasa_primary, intensity=sc.intensity)]
            camera = self.navarasa["rasas"][sc.rasa_primary]["camera"]
            shots = self.director.shots(bible, lock, sc, script, actions, camera)
            shots = self._clean_shots(shots.model_copy(update={"scene_id": sc.scene_id}), script, lock)
            shots = fit_shots_to_scene(shots, sc.target_seconds, fps=self._film["fps"])
            self._save(path, shots)
            self.log(f"  {sc.scene_id}: {len(shots.shots)} shots")

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
        if timeline_path.exists():
            previous = json.loads(timeline_path.read_text(encoding="utf-8"))
            if previous.get("only", []) == only and previous.get("takes", {}) == self.takes:
                return
            self.log("  shot selection or chosen takes changed: re-rendering (paid clips are reused)")
            self.invalidate("render")
        outline = self._load(self.p.outline, Outline)
        lock = self._lock()
        prov = self.cfg["providers"]
        video = get_provider("video", prov["video"], **self._provider_kwargs("video", prov["video"], lock))
        tts = get_provider("tts", prov["tts"], **self._provider_kwargs("tts", prov["tts"], lock))
        music = get_provider("music", prov["music"], **self._provider_kwargs("music", prov["music"], lock))
        face_guard = self._face_guard(lock)
        voice_guard, speaker_embedder = self._voice_guard(lock)
        resolution = tuple(self.cfg.get("animatic", {}).get("resolution", [640, 360])) if self._animatic() \
            else tuple(self._film["resolution"])
        fps = self._film["fps"]

        uses_inbox = prov["video"] in ("veo", "shotpack")     # paid / hand-made clips live in inbox/video
        shots_out, dialogue_out, spans = [], [], []
        starts = scene_start_times(outline)
        film_end = sum(s.target_seconds for s in outline.scenes)
        prev_end = -DIALOGUE_GAP_S                            # end of the last placed line, across shots and scenes
        for sc in outline.scenes:
            script = self._load(self.p.scenes / f"{sc.scene_id}.json", SceneScript)
            blocks = {b.block_id: b for b in script.blocks}
            shot_list = self._load(self.p.shots / f"{sc.scene_id}.json", ShotList)
            ctx = RenderContext(cast=lock, scene_id=sc.scene_id, resolution=resolution, fps=fps)
            t, placed = starts[sc.scene_id], set()
            for shot in shot_list.shots:
                take = self.takes.get(shot.shot_id, 1)
                paid = clips_for(self.p.root, shot.shot_id) if uses_inbox else []
                if selected(shot, sc.scene_id, only):
                    clip = self._render_shot(video, shot, ctx, face_guard)
                elif len(paid) >= take:                    # outside --only but already generated: use it, free
                    clip = ffmpeg.conform_clip(paid[take - 1], self.p.video / shot.shot_id / f"take_{take}.mp4",
                                               shot.duration_s, ctx.resolution, ctx.fps)
                else:
                    clip = MockVideo().render_shot(shot, self.p.video / shot.shot_id / "placeholder.mp4", ctx,
                                                   note="not generated yet")
                real = not uses_inbox or bool(clips_for(self.p.root, shot.shot_id))
                shots_out.append(assemble.TimelineShot(shot.shot_id, sc.scene_id, round(t, 3), shot.duration_s,
                                                       clip, real))
                spans.append((round(t, 3), shot))
                for bid in shot.block_ids:
                    block = blocks.get(bid)
                    if block is None or block.kind != "dialogue" or bid in placed:
                        continue
                    voice = lock.characters[block.character_id].voice_asset if block.character_id in lock.characters else None
                    wav, dur = self._render_line(tts, block, voice, voice_guard, speaker_embedder)
                    # Lines never overlap and are never cut: a long line runs on into the next shot or scene, and the
                    # next line waits for it. Only the film's last lines are pulled earlier to end with the film.
                    start = max(t + DIALOGUE_LEAD_IN_S, prev_end + DIALOGUE_GAP_S)
                    if start + dur > film_end:
                        start = max(prev_end + DIALOGUE_GAP_S, min(start, max(t, film_end - dur)))
                    start = round(start, 3)
                    dialogue_out.append(assemble.AudioEvent(wav, start, dur, "dialogue", ref_id=bid))
                    placed.add(bid)
                    prev_end = start + dur
                t += shot.duration_s

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
            "film_seconds": sum(s.target_seconds for s in outline.scenes),
            "shots": [{**asdict(s), "clip": str(s.clip)} for s in shots_out],
            "dialogue": [{**asdict(e), "path": str(e.path)} for e in dialogue_out],
            "music": [{**asdict(e), "path": str(e.path)} for e in music_out],
        }
        self.p.edit.mkdir(parents=True, exist_ok=True)
        timeline_path.write_text(json.dumps(timeline, indent=1), encoding="utf-8")
        self.log(f"  rendered {len(shots_out)} shots, {len(dialogue_out)} lines, {len(music_out)} cues")

    def _render_shot(self, video, shot, ctx: RenderContext, face_guard) -> Path:
        # Without a face guard you choose the take (takes.yaml, `nraiorbit retake`); with one, it retakes itself.
        first = 1 if face_guard else self.takes.get(shot.shot_id, 1)
        last = self.cfg["retakes"]["max_per_shot"] if face_guard else first
        clip, last_result = None, None
        for take in range(first, last + 1):
            clip = self.p.video / shot.shot_id / f"take_{take}.mp4"
            if not clip.exists():
                try:
                    video.render_shot(shot, clip, replace(ctx, seed=take))
                except ShotFiltered as blocked:
                    self._report("review_queue", {"shot_id": shot.shot_id, "reason": [str(blocked)]})
                    self.log(f"  {shot.shot_id}: blocked by the safety filter, placeholder used (see review_queue)")
                    return MockVideo().render_shot(shot, clip, ctx, note="BLOCKED by safety filter: rewrite shot")
                except (BudgetExceeded, GoogleAPIError) as stop:
                    raise PipelineBlocked(str(stop)) from None
                except FileNotFoundError as missing:
                    raise PipelineBlocked(str(missing)) from None
            if face_guard is None or not shot.characters_in_frame:
                return clip
            last_result = self._check_identity(face_guard, shot, clip)
            self._report("guards", {"guard": "identity", "shot_id": shot.shot_id, "take": take, **last_result.to_dict()})
            if last_result.passed:
                return clip
        self._report("review_queue", {"shot_id": shot.shot_id, "reason": last_result.issues if last_result else []})
        return clip

    def _render_line(self, tts, block, voice, voice_guard, speaker_embedder) -> tuple[Path, float]:
        takes = self.cfg["voice"]["takes_per_line"] if voice_guard else 1
        results, files = [], {}
        for take in range(1, takes + 1):
            wav = self.p.dialogue / block.block_id / f"take_{take}.wav"
            try:
                dur = ffmpeg.duration(wav) if wav.exists() else tts.synthesize(block, voice, wav)
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
        if not movie.exists():
            tl = json.loads((self.p.edit / "timeline.json").read_text(encoding="utf-8"))
            outline = self._load(self.p.outline, Outline)
            starts = scene_start_times(outline)
            shots = [assemble.TimelineShot(**{**s, "clip": Path(s["clip"])}) for s in tl["shots"]]
            dialogue = [assemble.AudioEvent(**{**e, "path": Path(e["path"])}) for e in tl["dialogue"]]
            music = [assemble.AudioEvent(**{**e, "path": Path(e["path"])}) for e in tl["music"]]
            scene_files = []
            for sc in outline.scenes:
                scene_shots = [s for s in shots if s.scene_id == sc.scene_id]
                start, end = starts[sc.scene_id], starts[sc.scene_id] + sc.target_seconds
                # every line overlapping the scene: one that runs past a cut is split there and joined seamlessly
                scene_dlg = [e for e in dialogue if e.start_s < end and e.start_s + e.duration_s > start]
                out = self.p.edit / "scenes" / f"{sc.scene_id}.mp4"
                scene_files.append(assemble.render_scene(scene_shots, scene_dlg, starts[sc.scene_id], out))
            film = assemble.join_scenes(scene_files, self.p.edit / "film_dialogue.mp4")
            audio = self.cfg["audio"]
            assemble.master(film, music, movie, audio["master_lufs"], audio["true_peak_db"])
            self._write_stems(film, movie)
            self._write_subtitles(dialogue)
            self.log(f"  master: {movie} ({ffmpeg.duration(movie) / 60:.1f} min)")
        self.gate("pacing" if self._animatic() else "final_cut")

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
            plan = plan_trailer(all_shots, bible, tl["film_seconds"], self._film["trailer_seconds"], lines,
                                max_line_s=self._film.get("trailer_line_max_s", 7.5),
                                max_lines=self._film.get("trailer_max_lines", 5), fps=fps)
            (self.p.edit / "trailer_plan.json").write_text(
                json.dumps([asdict(c) for c in plan], indent=1), encoding="utf-8")
            resolution = tuple(self.cfg.get("animatic", {}).get("resolution", [640, 360])) if self._animatic() \
                else tuple(self._film["resolution"])
            # Picture only: the film's sound would chop lines at every cut. Whole lines are laid over it instead.
            raw = assemble.cut_segments(
                self.p.edit / "film_dialogue.mp4",
                [(None if c.shot_id is None else c.film_start_s, c.duration_s) for c in plan],
                self.p.edit / "trailer_raw.mp4", title=bible.title_en, resolution=resolution, fps=fps, audio=False)
            total = round(sum(c.duration_s for c in plan), 4)
            wavs = {e["ref_id"]: Path(e["path"]) for e in tl["dialogue"]}
            dialogue = ffmpeg.place_audio([(wavs[c.line_id], c.line_at_s, 0.0) for c in plan if c.line_id], total,
                                          self.p.edit / ".trailer_dialogue" / "dialogue.wav")
            film = ffmpeg.mux(raw, dialogue, self.p.edit / "trailer_dialogue.mp4")
            notes, drop_at = elevation_arc(plan)
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
            assemble.master(film, [assemble.AudioEvent(wav, 0.0, total, "music", -2.0, "trailer")], out,
                            audio["master_lufs"], audio["true_peak_db"], music_dips=[(drop_at, PUNCH_DROP_S, -20.0)])
            self.log(f"  trailer: {out} ({total:.0f} s, {sum(c.shot_id is not None for c in plan)} clips, "
                     f"{sum(bool(c.line_id) for c in plan)} whole lines)")
        self.gate("trailer")

    def _write_trailer_cue(self, cue: Cue) -> None:
        """The trailer cue with its timed elevation arc, for review and for re-scoring by hand."""
        self._save(self.p.music / "trailer_cue.json", cue)
