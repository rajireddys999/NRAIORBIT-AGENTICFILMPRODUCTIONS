"""`nraiorbit` command line.

  nraiorbit new <slug> --story examples/story_godavari.md   create a project from a plot file
  nraiorbit run <slug> [--until STAGE] [--force STAGE]      run (or resume) the pipeline with Claude
  nraiorbit demo <slug>                                     offline demo: recorded agents + animatic render
  nraiorbit poc <slug>                                      5-minute POC project (screenplay + shots pre-written)
  nraiorbit estimate [<slug>] [--runtime 105]               bill estimate before (and actuals after) generating
  nraiorbit shotpack export|status <slug>                   Veo prompts per shot / which clips are in the inbox
  nraiorbit identity build <slug> [--only id1,id2]          character reference images (Gemini)
  nraiorbit run <slug> --only trailer [--max-spend 20]      paid run limited to trailer shots, with a budget cap
  nraiorbit retake <slug> <shot_id>... [--use N]            redo chosen shots (paid) or pick an earlier attempt (free)
  nraiorbit retake <slug> <shot_id>... --keep-keyframe      new motion from the same first frame (no new image)
  nraiorbit keyframes <slug> [--only sc003 sc004_s004]      first frames only, to check faces before paying for video
  nraiorbit google models                                   models your Google key can use
  nraiorbit approve <slug> <gate>                           approve a review gate
  nraiorbit status <slug>
  nraiorbit library validate [--strict]
  nraiorbit library search <kind> [--tags a b] [--rasa veera] [--intensity 8] [--bpm 130]
  nraiorbit publish <slug> --client-secrets client_secret.json [--privacy private]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import yaml

from .config import REPO_ROOT, load_pipeline_config
from .costing import BudgetExceeded, UnknownVideoTier
from .library.registry import AssetRegistry
from .pipeline import STAGES, GatePending, Pipeline, PipelineBlocked, ProjectPaths
from .providers.google_ai import GoogleAPIError, GoogleSetupError

PROJECTS = Path(__import__("os").environ.get("NRAIORBIT_PROJECTS", "projects"))   # the runner mounts its own


def _project(slug: str) -> Path:
    path = PROJECTS / slug
    if not path.exists():
        sys.exit(f"no project '{slug}' in {PROJECTS.resolve()} (create it with `nraiorbit new {slug}`)")
    return path


def cmd_new(args) -> None:
    root = PROJECTS / args.slug
    paths = ProjectPaths(root)
    if paths.story_input.exists():
        sys.exit(f"{paths.story_input} already exists")
    text = Path(args.story).read_text(encoding="utf-8") if args.story else args.prompt
    if not text:
        sys.exit("give --story FILE or --prompt TEXT")
    template = PROJECTS / "_template"
    if template.exists() and not args.bare:                 # the choices the last film settled on, as data
        shutil.copytree(template, root, dirs_exist_ok=True)
    paths.story_input.parent.mkdir(parents=True, exist_ok=True)
    paths.story_input.write_text(text, encoding="utf-8")
    print(f"created {root} from {'the template' if template.exists() and not args.bare else 'scratch'}. "
          f"Set budget.max_usd and voice.cast in {root / 'pipeline.yaml'}, then: nraiorbit produce {args.slug}")


def _run(pipeline: Pipeline, slug: str, until=None, force=()) -> None:
    try:
        pipeline.run(until=until, force=tuple(force or ()))
    except GatePending as g:
        print(f"\nPaused at review gate '{g.gate}'. Review the artifacts in {pipeline.p.root}, then:\n"
              f"  nraiorbit approve {slug} {g.gate}\n  nraiorbit run {slug}")
        return
    except (PipelineBlocked, BudgetExceeded) as b:      # paid music cues (render, trailer) raise the budget stop as is
        sys.exit(f"\nBlocked: {b}")
    except GoogleSetupError as e:
        sys.exit(f"\nGoogle setup needed: {e}")
    except GoogleAPIError as e:
        sys.exit(f"\nGoogle API error: {e}")
    if until is None:
        print(f"\nDone. Outputs in {pipeline.p.master}")


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} -> not confirmed (no terminal); re-run with --yes to proceed")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _apply_run_options(cfg: dict, args) -> dict:
    if getattr(args, "only", None) is not None:
        cfg.setdefault("render", {})["only"] = args.only
    if getattr(args, "max_spend", None) is not None:
        cfg.setdefault("budget", {})["max_usd"] = args.max_spend
    if getattr(args, "offline", False):
        cfg.setdefault("lipsync", {})["offline"] = True      # paid lip-sync results only, never a new job
        cfg.setdefault("music", {})["offline"] = True        # cached or inbox music only, never a Lyria call
    return cfg


def _preflight(root: Path, cfg: dict, assume_yes: bool) -> bool:
    """Show what the paid generators will cost before spending anything."""
    from .costing import SpendLedger, load_pricing, preview_run

    prov = cfg.get("providers", {})
    lyria = prov.get("music") == "lyria"
    paid = prov.get("video") == "veo" or prov.get("tts") in ("gemini", "elevenlabs") or lyria or (
        prov.get("tts") == "inbox" and cfg.get("voice", {}).get("inbox_fallback") in ("gemini", "elevenlabs")) or (
        prov.get("lipsync") == "sync_so" and not (cfg.get("lipsync") or {}).get("offline"))
    if not paid or not (root / "04_shots").exists():
        return True
    pre = preview_run(root, cfg, load_pricing(root))
    spent = SpendLedger(root / "reports" / "cost.jsonl", None).spent()
    budget = cfg.get("budget", {}).get("max_usd")
    only = cfg.get("render", {}).get("only") or []
    print(f"Paid generation for this run ({'only ' + ', '.join(only) if only else 'all shots'}):\n"
          f"  Veo: {pre['shots']} new clips, {pre['seconds']} s -> ${pre['video_usd']:.2f}"
          f"  ({pre['cached_shots']} shots already generated, reused for free)\n"
          f"  Keyframes: {pre['keyframes']} images -> ${pre['keyframe_usd']:.2f}\n"
          f"  Telugu voices (Gemini TTS): {pre['lines']} takes (a crowd line is one per voice) -> ${pre['tts_usd']:.2f}\n"
          + (f"  Music (Lyria): {pre['cues']} new cues -> ${pre['music_usd']:.2f}"
             f"  ({pre['cached_cues']} already generated or in inbox/music, free)\n" if lyria else "")
          + (f"  Lip-sync (sync.so): up to {pre['lipsync_seconds']:.0f} s -> ${pre['lipsync_usd']:.2f}"
             f"  ({pre['cached_lipsync_shots']} shots synced already; cached results are free)\n"
             if pre.get("lipsync_usd") else "")
          + f"  Total about ${pre['total_usd']:.2f} for one attempt per shot. Spent so far ${spent:.2f}"
          + (f", budget ${budget:.2f}." if budget is not None else ", no budget cap."))
    if pre["total_usd"] == 0:
        return True
    return _confirm("Proceed?", assume_yes)


def cmd_run(args) -> None:
    from .llm import ClaudeClient

    root = _project(args.slug)
    cfg = _apply_run_options(load_pipeline_config(root), args)
    if not _preflight(root, cfg, args.yes):
        return
    llm = ClaudeClient(model=cfg["llm"]["model"], max_tokens=cfg["llm"]["max_tokens"],
                       usage_log=root / "reports" / "llm_usage.jsonl")
    _run(Pipeline(root, llm, cfg=cfg), args.slug, args.until, args.force)


def _keep_keyframe(root: Path, shot_id: str, current: int, attempt: int) -> tuple[Path, Path] | None:
    """(keyframe of the attempt in use, else the newest; new attempt's keyframe) to copy, or None when that
    attempt already has one."""
    folder = root / "06_keyframes" / shot_id
    target = folder / f"take_{attempt}.png"
    if target.exists():
        return None
    if (folder / f"take_{current}.png").exists():
        return folder / f"take_{current}.png", target
    takes = sorted((int(p.stem.split("_")[1]), p) for p in folder.glob("take_*.png")
                   if p.stem.split("_")[1].isdigit()) if folder.is_dir() else []
    if not takes:
        sys.exit(f"{shot_id} has no keyframe to keep (shots without lead characters are text-to-video); "
                 "drop --keep-keyframe")
    return takes[-1][1], target


def cmd_retake(args) -> None:
    """Generate another attempt of chosen shots (or pick an earlier attempt with --use) and re-render."""
    from .costing import load_takes
    from .llm import ClaudeClient
    from .schemas import ShotList
    from .shotpack import clips_for

    root = _project(args.slug)
    known = {s.shot_id for p in (root / "04_shots").glob("*.json")
             for s in ShotList.model_validate_json(p.read_text(encoding="utf-8")).shots}
    unknown = [sid for sid in args.shots if sid not in known]
    if unknown:
        sys.exit(f"unknown shot ids: {', '.join(unknown)}")
    takes_path = root / "takes.yaml"
    before = takes_path.read_text(encoding="utf-8") if takes_path.exists() else None
    takes = load_takes(root)
    if args.keep_keyframe and args.use is not None:
        sys.exit("--keep-keyframe is for a new attempt; --use picks an existing one")
    copies: list[tuple[Path, Path]] = []
    for sid in args.shots:
        have, keep = len(clips_for(root, sid)), None
        if have == 0:
            sys.exit(f"{sid} has not been generated yet; run it first (e.g. nraiorbit run {args.slug} --only {sid})")
        if args.use is not None:
            if not 1 <= args.use <= have:
                sys.exit(f"{sid} has {have} attempt(s); --use must be between 1 and {have}")
            takes[sid] = args.use
        else:
            current = takes.get(sid, 1)
            takes[sid] = max(current, have) + 1
            keep = _keep_keyframe(root, sid, current, takes[sid]) if args.keep_keyframe else None
            if keep:
                copies.append(keep)
        print(f"  {sid}: using attempt {takes[sid]} ({have} already generated)"
              + (f", first frame from {keep[0].name}" if keep else ""))
    takes_path.write_text(yaml.safe_dump(takes, sort_keys=True), encoding="utf-8")
    for src, dest in copies:                                   # the new attempt animates the same still: no image paid
        shutil.copy2(src, dest)
    cfg = _apply_run_options(load_pipeline_config(root), args)
    cfg.setdefault("render", {})["only"] = list(args.shots)    # generate only these; other paid clips are reused

    def undo() -> None:
        if before is None:
            takes_path.unlink(missing_ok=True)
        else:
            takes_path.write_text(before, encoding="utf-8")
        for _, dest in copies:
            dest.unlink(missing_ok=True)

    try:
        approved = _preflight(root, cfg, args.yes)
    except BaseException:                                      # e.g. an unknown video_tier: queue nothing unconfirmed
        undo()
        raise
    if not approved:
        undo()
        return
    llm = ClaudeClient(model=cfg["llm"]["model"], max_tokens=cfg["llm"]["max_tokens"],
                       usage_log=root / "reports" / "llm_usage.jsonl")
    _run(Pipeline(root, llm, cfg=cfg), args.slug)


def cmd_identity(args) -> None:
    from .costing import SpendLedger, load_pricing
    from .identity import build_identity_pack, images_to_make, plan_identity_pack
    from .llm import ClaudeClient
    from .providers.google_ai import GeminiImages, google_client
    from .schemas import CastLock, StoryBible

    root = _project(args.slug)
    cfg = load_pipeline_config(root)
    if args.max_spend is not None:
        cfg.setdefault("budget", {})["max_usd"] = args.max_spend
    paths = ProjectPaths(root)
    if not paths.lock.exists():
        _run(Pipeline(root, ClaudeClient(), cfg=cfg), args.slug, until="cast")
    bible = StoryBible.model_validate_json(paths.bible.read_text(encoding="utf-8"))
    lock = CastLock.model_validate(yaml.safe_load(paths.lock.read_text(encoding="utf-8")))
    pricing = load_pricing(root)
    library = AssetRegistry.load(REPO_ROOT / "library")
    only = [c.strip() for c in (args.only or "").split(",") if c.strip()] or None
    try:
        jobs = plan_identity_pack(bible, lock, library, only=only, log=lambda m: None)
    except (KeyError, ValueError) as e:
        sys.exit(f"identity build: {e.args[0] if e.args else e}")
    n_images = len(images_to_make(jobs, args.force))
    price = pricing.get("gemini", {}).get("image_per_image", 0.04)
    who = f" for {', '.join(job.spec.id for job in jobs) or 'nobody'}" if only else ""
    if not n_images:
        print(f"Character reference images{who}: nothing to generate (existing images are kept; --force regenerates)")
        return
    print(f"Character reference images{who}: {n_images} image(s) "
          f"({'existing ones are regenerated' if args.force else 'existing ones are kept; --force regenerates'})"
          f" -> at most ${n_images * price:.2f}")
    if not _confirm("Generate them with Gemini?", args.yes):
        return
    ledger = SpendLedger(paths.reports / "cost.jsonl", cfg.get("budget", {}).get("max_usd"))
    images = GeminiImages(google_client(cfg.get("google", {}), "image"), cfg.get("google", {}), pricing, ledger)
    build_identity_pack(bible, lock, library, images, force=args.force, only=only)
    print("Review the images in library/characters/*/refs/ before generating video; delete any you dislike and run "
          "this again to regenerate just those.")


def cmd_keyframes(args) -> None:
    """First frames only (keyframe mode): check every face before any video is paid for. Delete a still you reject
    and run this again to make just that one; `run` then animates the stills that are there."""
    from .config import load_navarasa
    from .costing import SpendLedger, load_pricing, load_takes
    from .providers.google_ai import ShotFiltered, VeoVideo
    from .schemas import CastLock, ShotList
    from .shotpack import clips_for, selected

    root = _project(args.slug)
    cfg = load_pipeline_config(root)
    if args.max_spend is not None:
        cfg.setdefault("budget", {})["max_usd"] = args.max_spend
    g = cfg.get("google", {})
    if g.get("veo_mode", "keyframe") == "reference":
        sys.exit("keyframes: google.veo_mode is 'reference', which uses no first frames")
    paths = ProjectPaths(root)
    lock = CastLock.model_validate(yaml.safe_load(paths.lock.read_text(encoding="utf-8")))
    takes = load_takes(root)
    todo = []                                     # (scene_id, shot, attempt) with no still and no clip yet
    for path in sorted((root / "04_shots").glob("*.json")):
        shot_list = ShotList.model_validate_json(path.read_text(encoding="utf-8"))
        for shot in shot_list.shots:
            attempt = takes.get(shot.shot_id, 1)
            if (not shot.characters_in_frame or not selected(shot, shot_list.scene_id, args.only)
                    or len(clips_for(root, shot.shot_id)) >= attempt
                    or (root / "06_keyframes" / shot.shot_id / f"take_{attempt}.png").exists()):
                continue
            todo.append((shot_list.scene_id, shot, attempt))
    pricing = load_pricing(root)
    price = pricing.get("gemini", {}).get("image_per_image", 0.04)
    if not todo:
        print("Keyframes: nothing to generate (every selected shot with characters has a still or a clip)")
        return
    print(f"Keyframes: {len(todo)} image(s) -> ${len(todo) * price:.2f}. No video is generated.")
    if not _confirm("Generate them?", args.yes):
        return
    ledger = SpendLedger(paths.reports / "cost.jsonl", cfg.get("budget", {}).get("max_usd"))
    video = VeoVideo(root, AssetRegistry.load(REPO_ROOT / "library"), cfg, pricing, ledger, load_navarasa())
    made = 0
    for scene_id, shot, attempt in todo:
        try:
            video.make_keyframe(shot, scene_id, lock, attempt)
            made += 1
        except ShotFiltered as blocked:
            print(f"  {shot.shot_id}: blocked ({blocked}); adjust its image_prompt and run again")
    print(f"{made} keyframe(s) in {root / '06_keyframes'}. Check them; delete any you reject and run this again.")


def cmd_google(args) -> None:
    from .providers.google_ai import _call, google_client

    g = dict(load_pipeline_config().get("google", {}))
    if args.vertex:
        g["vertex"] = True
    client = google_client(g)
    wanted = ("veo", "image", "tts", "lyria")
    print(f"Endpoint: {'Vertex AI' if g.get('vertex') else 'Gemini API'}")
    for m in _call(client.models.list, "list models"):
        name = getattr(m, "name", str(m))
        if any(w in name for w in wanted):
            print(f"  {name}  {getattr(m, 'display_name', '') or ''}")


def cmd_demo(args) -> None:
    from .demo import DEMO_CONFIG, DEMO_PLOT, demo_llm

    root = PROJECTS / args.slug
    paths = ProjectPaths(root)
    paths.story_input.parent.mkdir(parents=True, exist_ok=True)
    if not paths.story_input.exists():
        paths.story_input.write_text(DEMO_PLOT, encoding="utf-8")
        (root / "pipeline.yaml").write_text(yaml.safe_dump(DEMO_CONFIG, sort_keys=False), encoding="utf-8")
    _run(Pipeline(root, demo_llm()), args.slug, args.until, args.force)


def cmd_poc(args) -> None:
    from .poc import create_poc

    root = PROJECTS / args.slug
    create_poc(root, automatic=not args.manual)
    print(f"created the 5-minute POC in {root}: story, screenplay, 54 shots and cue sheet are written.")
    if args.manual:
        print(f"Next (manual Veo):\n  nraiorbit estimate {args.slug}\n  nraiorbit shotpack export {args.slug}\n"
              f"  (generate clips into {root / 'inbox' / 'video'})\n  nraiorbit run {args.slug} --force render")
    else:
        print(f"Next (automatic, needs GEMINI_API_KEY):\n  nraiorbit estimate {args.slug}\n"
              f"  nraiorbit identity build {args.slug}        # character reference images (~$1.50)\n"
              f"  nraiorbit run {args.slug} --only trailer    # cheap first test: trailer shots only\n"
              f"  nraiorbit run {args.slug}                   # the full 5 minutes (reuses paid clips)")
    print("See docs/POC_RUNBOOK.md.")


def cmd_estimate(args) -> None:
    from .costing import estimate, load_pricing, render_markdown

    root = _project(args.slug) if args.slug else None
    pricing = load_pricing(root)
    report = render_markdown(estimate(root, runtime_minutes=args.runtime, pricing=pricing), pricing)
    print(report)
    if root is not None:
        (root / "reports").mkdir(exist_ok=True)
        (root / "reports" / "cost_estimate.md").write_text(report, encoding="utf-8")


def cmd_shotpack(args) -> None:
    from . import shotpack
    from .config import load_navarasa
    from .costing import load_pricing
    from .llm import ClaudeClient

    root = _project(args.slug)
    if args.action == "export":
        if not ProjectPaths(root).lock.exists():
            # casting is library-first; Claude is only called for characters the library lacks
            _run(Pipeline(root, ClaudeClient()), args.slug, until="cast")
        pack = shotpack.export(root, AssetRegistry.load(REPO_ROOT / "library"), load_navarasa(), load_pricing(root),
                               load_pipeline_config(root).get("google", {}))
        print(f"shot pack written to {pack}/SHOTPACK.md; save clips into {shotpack.inbox_dir(root, 'video')}")
    else:
        st = shotpack.status(root)
        total = len(st["done"]) + len(st["missing"])
        print(f"{len(st['done'])}/{total} shots have clips ({st['attempts']} attempts in inbox/video)")
        if st["missing"]:
            print("missing: " + ", ".join(st["missing"]))


def cmd_approve(args) -> None:
    from .service import queue
    queue.approve_gate(_project(args.slug), args.gate)       # keeps what the marker said (the cost gate's estimate)
    print(f"approved '{args.gate}'. Continue with: nraiorbit run {args.slug}")


def cmd_status(args) -> None:
    pipeline = Pipeline(_project(args.slug), llm=None)   # status needs no LLM
    for stage, state in pipeline.status().items():
        print(f"  {stage:<12} {state}")


def cmd_library(args) -> None:
    lib = AssetRegistry.load(REPO_ROOT / "library")
    if args.action == "validate":
        report = lib.validate(strict_files=args.strict)
        print(f"{len(lib.assets)} assets")
        for w in report.warnings:
            print(f"  warning: {w}")
        for e in report.errors:
            print(f"  ERROR: {e}")
        sys.exit(0 if report.ok else 1)
    hits = lib.search(args.kind, tags=args.tags, rasa=args.rasa, intensity=args.intensity, bpm=args.bpm, k=args.k)
    for h in hits:
        print(f"  {h.score:.3f}  {lib.ref(h.asset)}  {h.asset.name or ''}  tags={h.asset.tags}")


def cmd_publish(args) -> None:
    from .publish import youtube
    from .schemas import Outline, StoryBible

    paths = ProjectPaths(_project(args.slug))
    bible = StoryBible.model_validate_json(paths.bible.read_text(encoding="utf-8"))
    outline = Outline.model_validate_json(paths.outline.read_text(encoding="utf-8"))
    video = paths.master / ("trailer.mp4" if args.trailer else "movie.mp4")
    meta = youtube.build_metadata(bible, outline, privacy=args.privacy)
    if args.trailer:
        meta["snippet"]["title"] = f"{bible.title_te} ({bible.title_en}) | Official Trailer"[:100]
    if args.dry_run:
        print(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False))
        return
    creds = youtube.credentials(Path(args.client_secrets), paths.root / ".youtube_token.json",
                                interactive=bool(getattr(args, "auth", False)) or sys.stdin.isatty())
    captions = {lang: paths.master / f"subs_{lang}.srt" for lang in ("te", "en")
                if (paths.master / f"subs_{lang}.srt").exists() and not args.trailer}
    thumb = paths.master / "thumbnail.png"
    video_id = youtube.upload(video, meta, creds, captions=captions, thumbnail=thumb if thumb.exists() else None)
    print(f"uploaded as {args.privacy}: https://www.youtube.com/watch?v={video_id}")


def _bucket(root: Path, args) -> str:
    from .release import gcs
    return getattr(args, "bucket", None) or gcs.default_bucket(load_pipeline_config(root))


def cmd_qa(args) -> None:
    root = _project(args.slug)
    if args.action == "lipsync":
        from .qa import lipsync
        rows = lipsync.review(root, shots=args.shots or None, strips=not args.no_strips)
        flagged = [r for r in rows if r.get("flags")]
        print(f"{len(rows)} shots measured, {len(flagged)} worth a look; sheets in {root / 'reports' / 'qa' / 'lipsync'}")
    else:
        from .qa import film
        report = film.check(root, master=Path(args.master) if args.master else None)
        out = root / "reports" / "qa" / "film.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(__import__("json").dumps(report, indent=1), encoding="utf-8")
        print("ok" if report.get("ok") else "LOOK: the film check found something; see " + str(out))


def cmd_release(args) -> None:
    if args.action == "youtube":
        cmd_publish(args)
        return
    from .release import deliver
    root = _project(args.slug)
    records = deliver.deliver(root, bucket=None if args.no_upload else _bucket(root, args), date=args.date,
                              tag=args.tag, days=args.days, parts=args.parts, upload=not args.no_upload)
    print(f"{len(records)} files" + ("" if args.no_upload else f", links valid {args.days} days"))


def cmd_produce(args) -> None:
    from .llm import ClaudeClient
    from .service import produce

    root = _project(args.slug)
    cfg = _apply_run_options(load_pipeline_config(root), args)
    llm = ClaudeClient(model=cfg["llm"]["model"], max_tokens=cfg["llm"]["max_tokens"],
                       usage_log=root / "reports" / "llm_usage.jsonl")
    report = produce.produce(Pipeline(root, llm, cfg=cfg), bucket=args.bucket, publish=args.publish,
                             gates=args.gates, keyframes=cmd_keyframes)
    print(f"\n{args.slug}: {report['state']}  (ledger ${report['ledger_usd']:.2f}; report in reports/produce.json)")
    if report["state"].startswith("awaiting:"):
        gate = report["state"].split(":", 1)[1]
        print(f"review it, then: nraiorbit approve {args.slug} {gate}  and  nraiorbit produce {args.slug}")


def cmd_learn(args) -> None:
    from .service import learn
    cfg = load_pipeline_config(_project(args.project) if args.project else None)
    from .costing import load_pricing
    for url in args.urls:
        path = learn.learn(url, title=args.title, what=args.learn, cfg=cfg, start_s=args.start, end_s=args.end,
                           minutes=args.minutes, max_usd=args.max_usd, yes=args.yes, pricing=load_pricing(None))
        print(f"  {url} -> {path}")


def cmd_service(args) -> None:
    from .service import learn, produce, queue
    if args.action == "tick":
        from .llm import ClaudeClient

        def produce_fn(root: Path, bucket):
            cfg = load_pipeline_config(root)
            llm = ClaudeClient(model=cfg["llm"]["model"], max_tokens=cfg["llm"]["max_tokens"],
                               usage_log=root / "reports" / "llm_usage.jsonl")
            return produce.produce(Pipeline(root, llm, cfg=cfg), bucket=bucket, keyframes=cmd_keyframes)

        def learn_fn(url, title, what, minutes=None):
            from .costing import load_pricing
            return learn.learn(url, title=title, what=what, cfg=load_pipeline_config(None),
                               minutes=float(minutes) if minutes else None, pricing=load_pricing(None))

        out = queue.tick(args.stories, args.references, PROJECTS, bucket=args.bucket, produce_fn=produce_fn,
                         learn_fn=learn_fn, template=PROJECTS / "_template")
        print(out)
        return
    if args.action == "add-story":
        rows = queue.load(args.stories, queue.STORY_COLUMNS)
        rows.append({"slug": args.slug, "story_uri": args.story_uri, "budget_usd": args.budget or "",
                     "gates": args.gates or "", "publish": args.publish or "", "state": "new", "note": "",
                     "updated": queue.now()})
        queue.save(args.stories, rows, queue.STORY_COLUMNS)
        print(f"queued story {args.slug} in {args.stories}")
        return
    rows = queue.load(args.references, queue.REFERENCE_COLUMNS)
    rows.append({"url": args.url, "title": args.title or "", "learn": args.learn or "all",
                 "minutes": args.minutes or "", "state": "new", "profile_uri": "", "note": "", "updated": queue.now()})
    queue.save(args.references, rows, queue.REFERENCE_COLUMNS)
    print(f"queued reference {args.url} in {args.references}")


def cmd_ui(args) -> None:
    """The control room: a private web page over the bucket (or a local projects folder) and the queue sheets."""
    import uvicorn

    from .ui.app import Settings, create_app
    from .ui.store import Store
    root = args.root or os.environ.get("UI_ROOT") or (
        f"gs://{os.environ['GOOGLE_CLOUD_PROJECT']}-media" if os.environ.get("GOOGLE_CLOUD_PROJECT") else str(PROJECTS))
    emails = {e.strip().lower() for e in (args.allow or os.environ.get("UI_ALLOWED_EMAILS", "")).split(",") if e.strip()}
    auth = not args.no_auth and os.environ.get("UI_AUTH", "on").lower() not in ("off", "0", "false")
    if auth and not emails:
        sys.exit("ui: give --allow <email>[,<email>] (or UI_ALLOWED_EMAILS), or --no-auth on a laptop")
    settings = Settings(allowed_emails=emails, auth=auth, project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                        region=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
                        job=os.environ.get("UI_JOB", "nraiorbit-runner") if os.environ.get("GOOGLE_CLOUD_PROJECT") else None,
                        google_client_id=os.environ.get("UI_GOOGLE_CLIENT_ID") or None,
                        google_client_secret=os.environ.get("UI_GOOGLE_CLIENT_SECRET") or None,
                        passphrase=(os.environ.get("UI_PASSPHRASE") or "").strip() or None,
                        session_secret=os.environ.get("UI_SESSION_SECRET") or None,
                        public_url=os.environ.get("UI_PUBLIC_URL") or None)
    if auth and not settings.google_client_id and not settings.passphrase:
        print("ui: no UI_GOOGLE_CLIENT_ID and no UI_PASSPHRASE; sign-in works only behind Identity-Aware Proxy")
    print(f"control room on http://{args.host}:{args.port} over {root}"
          + ("" if auth else "  (no sign-in: local use only)"))
    uvicorn.run(create_app(Store(root), settings), host=args.host, port=int(args.port), log_level="warning",
                proxy_headers=True, forwarded_allow_ips="*")      # Cloud Run's front door terminates TLS


def cmd_backup(args) -> None:
    from .ops import backup
    root = _project(args.slug)
    fn = backup.restore if args.cmd == "restore" else backup.backup
    n = fn(root, _bucket(root, args), sets=args.sets or None)
    print(f"{args.cmd}: {n} sets")


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    except GoogleSetupError as e:
        sys.exit(f"Google setup needed: {e}")
    except GoogleAPIError as e:
        sys.exit(f"Google API error: {e}")
    except UnknownVideoTier as e:
        sys.exit(str(e))


def _main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="nraiorbit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="a project from projects/_template and a story")
    p.add_argument("slug")
    p.add_argument("--story")
    p.add_argument("--prompt")
    p.add_argument("--bare", action="store_true", help="only the story; no template")
    p.set_defaults(fn=cmd_new)

    for name, fn in (("run", cmd_run), ("demo", cmd_demo)):
        p = sub.add_parser(name)
        p.add_argument("slug")
        p.add_argument("--until", choices=STAGES)
        p.add_argument("--force", nargs="*", choices=STAGES, default=[])
        if name == "run":
            p.add_argument("--only", nargs="*",
                           help="generate only these scene ids, shot ids and/or 'trailer'; other shots reuse paid clips")
            p.add_argument("--max-spend", type=float, help="budget cap in USD for paid generation (overrides config)")
            p.add_argument("--yes", action="store_true", help="skip the spend confirmation")
            p.add_argument("--offline", action="store_true", help="lip-sync from paid results only; never a new job")
        p.set_defaults(fn=fn)

    p = sub.add_parser("retake", help="new attempt for chosen shots (paid), or --use N to pick an earlier one (free)")
    p.add_argument("slug")
    p.add_argument("shots", nargs="+", help="shot ids, e.g. sc002_s005")
    p.add_argument("--use", type=int, help="use this existing attempt instead of generating a new one")
    p.add_argument("--keep-keyframe", action="store_true",
                   help="motion-only retake: animate the keyframe of the attempt in use (else the newest) again "
                        "instead of paying for a new one")
    p.add_argument("--max-spend", type=float)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_retake)

    p = sub.add_parser("identity", help="generate character reference images with Gemini")
    p.add_argument("action", choices=["build"])
    p.add_argument("slug")
    p.add_argument("--force", action="store_true", help="regenerate existing images")
    p.add_argument("--only", help="comma-separated Story Bible character ids, e.g. raghavaiah_father,kaasi_young")
    p.add_argument("--max-spend", type=float)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_identity)

    p = sub.add_parser("keyframes", help="first frames only (paid images, no video), to check faces before Veo")
    p.add_argument("slug")
    p.add_argument("--only", nargs="*", help="scene ids and/or shot ids (default: every shot)")
    p.add_argument("--max-spend", type=float)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_keyframes)

    p = sub.add_parser("google", help="list the Veo / image / TTS models your Google key can use")
    p.add_argument("action", choices=["models"])
    p.add_argument("--vertex", action="store_true", help="use the key through Vertex AI (express mode) instead")
    p.set_defaults(fn=cmd_google)

    p = sub.add_parser("poc", help="create the 5-minute Godavari Simham POC project")
    p.add_argument("slug")
    p.add_argument("--manual", action="store_true", help="generate Veo clips by hand (shot pack) instead of the API")
    p.set_defaults(fn=cmd_poc)

    p = sub.add_parser("estimate", help="bill estimate for a project, or for a runtime")
    p.add_argument("slug", nargs="?")
    p.add_argument("--runtime", type=float, help="minutes, when there is no project/shot plan")
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("shotpack", help="Veo prompts + references per shot, and import status")
    p.add_argument("action", choices=["export", "status"])
    p.add_argument("slug")
    p.set_defaults(fn=cmd_shotpack)

    p = sub.add_parser("approve")
    p.add_argument("slug")
    p.add_argument("gate", choices=["story", "outline", "scenes", "cast", "shots", "cost", "faces", "render", "pacing",
                                    "final_cut", "trailer", "publish"])
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("status")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("library")
    lib_sub = p.add_subparsers(dest="action", required=True)
    v = lib_sub.add_parser("validate")
    v.add_argument("--strict", action="store_true", help="missing media files are errors")
    s = lib_sub.add_parser("search")
    s.add_argument("kind", choices=["character", "voice", "music_stem", "motif", "action", "location", "sfx"])
    s.add_argument("--tags", nargs="*", default=[])
    s.add_argument("--rasa")
    s.add_argument("--intensity", type=int)
    s.add_argument("--bpm", type=int)
    s.add_argument("-k", type=int, default=5)
    p.set_defaults(fn=cmd_library)

    p = sub.add_parser("publish")
    p.add_argument("slug")
    p.add_argument("--client-secrets", default="client_secret.json")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    p.add_argument("--trailer", action="store_true", help="upload the trailer instead of the film")
    p.add_argument("--dry-run", action="store_true", help="print the metadata without uploading")
    p.add_argument("--auth", action="store_true", help="sign in through the browser once and store the token")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("produce", help="one story start to finish under the project's policy: plan, faces, render, "
                                       "QA, deliver, YouTube, backup")
    p.add_argument("slug")
    p.add_argument("--max-spend", type=float, help="budget cap in USD (overrides config)")
    p.add_argument("--gates", choices=["ask", "auto"], help="stop at review gates (ask) or approve them (auto)")
    p.add_argument("--publish", choices=["none", "private", "unlisted", "public"], help="YouTube privacy, or none")
    p.add_argument("--bucket", help="deliver and back up to this bucket (default from the config)")
    p.add_argument("--offline", action="store_true", help="lip-sync from paid results only")
    p.set_defaults(fn=cmd_produce)

    p = sub.add_parser("qa", help="quality checks after a render: lipsync (every speaking shot) or film (the master)")
    qa_sub = p.add_subparsers(dest="action", required=True)
    q = qa_sub.add_parser("lipsync")
    q.add_argument("slug")
    q.add_argument("--shots", nargs="*", help="only these shot ids")
    q.add_argument("--no-strips", action="store_true", help="skip the frame strips (faster)")
    q = qa_sub.add_parser("film")
    q.add_argument("slug")
    q.add_argument("--master", help="another file than 11_master/movie.mp4")
    p.set_defaults(fn=cmd_qa)

    p = sub.add_parser("release", help="deliver (720p copy, bucket upload, signed links) or youtube (upload)")
    rel_sub = p.add_subparsers(dest="action", required=True)
    d = rel_sub.add_parser("deliver")
    d.add_argument("slug")
    d.add_argument("--bucket")
    d.add_argument("--date", help="date in the file names (default today)")
    d.add_argument("--tag", default="", help="a word in the file names, e.g. voiceE")
    d.add_argument("--days", type=int, default=7, help="how long the download links live")
    d.add_argument("--parts", type=int, default=0, help="also cut the film into this many 720p parts")
    d.add_argument("--no-upload", action="store_true", help="make the files only")
    y = rel_sub.add_parser("youtube")
    y.add_argument("slug")
    y.add_argument("--client-secrets", default="client_secret.json")
    y.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    y.add_argument("--trailer", action="store_true", help="upload the trailer instead of the film")
    y.add_argument("--dry-run", action="store_true", help="print the metadata without uploading")
    y.add_argument("--auth", action="store_true", help="sign in through the browser once and store the token")
    p.set_defaults(fn=cmd_release)

    p = sub.add_parser("learn", help="watch public films with Gemini and write craft notes the agents use as examples")
    learn_sub = p.add_subparsers(dest="action", required=True)
    yt = learn_sub.add_parser("youtube")
    yt.add_argument("urls", nargs="+")
    yt.add_argument("--title")
    yt.add_argument("--learn", default="all", choices=["all", "score", "emotion", "action", "characters", "trailer"])
    yt.add_argument("--start", type=float, help="seconds into the video to start watching")
    yt.add_argument("--end", type=float, help="seconds into the video to stop")
    yt.add_argument("--project", help="read google.* settings from this project instead of the defaults")
    yt.add_argument("--minutes", type=float, help="the video's length, for the cost estimate (default 150)")
    yt.add_argument("--max-usd", type=float, default=1.0, help="stop if the estimate is above this (default $1)")
    yt.add_argument("--yes", action="store_true", help="watch even if the estimate is above --max-usd")
    p.set_defaults(fn=cmd_learn)

    p = sub.add_parser("service", help="the queue runner: tick (one pass), add-story, add-reference")
    svc = p.add_subparsers(dest="action", required=True)
    t = svc.add_parser("tick")
    t.add_argument("--stories", required=True, help="queue CSV, local path or gs://bucket/queue/stories.csv")
    t.add_argument("--references", required=True, help="queue CSV for reference films")
    t.add_argument("--bucket", help="restore and back up projects here")
    a = svc.add_parser("add-story")
    a.add_argument("slug")
    a.add_argument("story_uri", help="gs://… or a local path to the story text")
    a.add_argument("--stories", required=True)
    a.add_argument("--budget", type=float)
    a.add_argument("--gates", choices=["ask", "auto"])
    a.add_argument("--publish", choices=["none", "private", "unlisted", "public"])
    r = svc.add_parser("add-reference")
    r.add_argument("url")
    r.add_argument("--references", required=True)
    r.add_argument("--title")
    r.add_argument("--learn", choices=["all", "score", "emotion", "action", "characters", "trailer"])
    r.add_argument("--minutes", type=float, help="the video's length, for the cost guard (else 150 is assumed)")
    p.set_defaults(fn=cmd_service)

    u = sub.add_parser("ui", help="the control room: a private web page over the bucket and the queue sheets")
    u.add_argument("--root", help="gs://bucket or a local projects folder (default: the project's media bucket)")
    u.add_argument("--host", default="0.0.0.0")
    u.add_argument("--port", default=os.environ.get("PORT", "8080"))
    u.add_argument("--allow", help="e-mail addresses allowed in, comma separated (behind IAP on Cloud Run)")
    u.add_argument("--no-auth", action="store_true", help="no sign-in check: a laptop, never the internet")
    u.set_defaults(fn=cmd_ui)

    for name in ("backup", "restore"):
        where = "to" if name == "backup" else "from"
        p = sub.add_parser(name, help=f"{name} a project's media, caches, reports and plan files {where} the bucket")
        p.add_argument("slug")
        p.add_argument("--bucket")
        p.add_argument("--sets", nargs="*", help="inbox cache keyframes music reports master scenes shots post files")
        p.set_defaults(fn=cmd_backup)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
