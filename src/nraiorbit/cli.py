"""`nraiorbit` command line.

  nraiorbit new <slug> --story examples/story_godavari.md   create a project from a plot file
  nraiorbit run <slug> [--until STAGE] [--force STAGE]      run (or resume) the pipeline with Claude
  nraiorbit demo <slug>                                     offline demo: recorded agents + animatic render
  nraiorbit poc <slug>                                      5-minute POC project (screenplay + shots pre-written)
  nraiorbit estimate [<slug>] [--runtime 105]               bill estimate before (and actuals after) generating
  nraiorbit shotpack export|status <slug>                   Veo prompts per shot / which clips are in the inbox
  nraiorbit identity build <slug>                           character reference images (Gemini)
  nraiorbit run <slug> --only trailer [--max-spend 20]      paid run limited to trailer shots, with a budget cap
  nraiorbit retake <slug> <shot_id>... [--use N]            redo chosen shots (paid) or pick an earlier attempt (free)
  nraiorbit google models                                   models your Google key can use
  nraiorbit approve <slug> <gate>                           approve a review gate
  nraiorbit status <slug>
  nraiorbit library validate [--strict]
  nraiorbit library search <kind> [--tags a b] [--rasa veera] [--intensity 8] [--bpm 130]
  nraiorbit publish <slug> --client-secrets client_secret.json [--privacy private]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .config import REPO_ROOT, load_pipeline_config
from .costing import BudgetExceeded
from .library.registry import AssetRegistry
from .pipeline import STAGES, GatePending, Pipeline, PipelineBlocked, ProjectPaths
from .providers.google_ai import GoogleAPIError, GoogleSetupError

PROJECTS = Path("projects")


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
    paths.story_input.parent.mkdir(parents=True, exist_ok=True)
    paths.story_input.write_text(text, encoding="utf-8")
    print(f"created {root}. Override settings in {root / 'pipeline.yaml'} if needed, then: nraiorbit run {args.slug}")


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
    return cfg


def _preflight(root: Path, cfg: dict, assume_yes: bool) -> bool:
    """Show what the paid generators will cost before spending anything."""
    from .costing import SpendLedger, load_pricing, preview_run

    prov = cfg.get("providers", {})
    lyria = prov.get("music") == "lyria"
    paid = prov.get("video") == "veo" or prov.get("tts") == "gemini" or lyria or (
        prov.get("tts") == "inbox" and cfg.get("voice", {}).get("inbox_fallback") == "gemini")
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
          f"  Telugu voices (Gemini TTS): {pre['lines']} lines -> ${pre['tts_usd']:.2f}\n"
          + (f"  Music (Lyria): {pre['cues']} new cues -> ${pre['music_usd']:.2f}"
             f"  ({pre['cached_cues']} already generated or in inbox/music, free)\n" if lyria else "")
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
    for sid in args.shots:
        have = len(clips_for(root, sid))
        if have == 0:
            sys.exit(f"{sid} has not been generated yet; run it first (e.g. nraiorbit run {args.slug} --only {sid})")
        if args.use is not None:
            if not 1 <= args.use <= have:
                sys.exit(f"{sid} has {have} attempt(s); --use must be between 1 and {have}")
            takes[sid] = args.use
        else:
            takes[sid] = max(takes.get(sid, 1), have) + 1
        print(f"  {sid}: using attempt {takes[sid]} ({have} already generated)")
    takes_path.write_text(yaml.safe_dump(takes, sort_keys=True), encoding="utf-8")
    cfg = _apply_run_options(load_pipeline_config(root), args)
    cfg.setdefault("render", {})["only"] = list(args.shots)    # generate only these; other paid clips are reused
    if not _preflight(root, cfg, args.yes):
        if before is None:
            takes_path.unlink()
        else:
            takes_path.write_text(before, encoding="utf-8")
        return
    llm = ClaudeClient(model=cfg["llm"]["model"], max_tokens=cfg["llm"]["max_tokens"],
                       usage_log=root / "reports" / "llm_usage.jsonl")
    _run(Pipeline(root, llm, cfg=cfg), args.slug)


def cmd_identity(args) -> None:
    from .costing import SpendLedger, load_pricing
    from .identity import build_identity_pack
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
    n_images = sum(2 + len(getattr(library.resolve(e.character_asset), "costumes", []))
                   for e in lock.characters.values() if not e.character_asset.endswith("@pending"))
    price = pricing.get("gemini", {}).get("image_per_image", 0.04)
    print(f"Character reference images: up to {n_images} images (existing ones are kept"
          f"{', except with --force' if not args.force else ''}) -> at most ${n_images * price:.2f}")
    if not _confirm("Generate them with Gemini?", args.yes):
        return
    ledger = SpendLedger(paths.reports / "cost.jsonl", cfg.get("budget", {}).get("max_usd"))
    images = GeminiImages(google_client(cfg.get("google", {}), "image"), cfg.get("google", {}), pricing, ledger)
    build_identity_pack(bible, lock, library, images, force=args.force)
    print("Review the images in library/characters/*/refs/ before generating video; delete any you dislike and run "
          "this again to regenerate just those.")


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
        pack = shotpack.export(root, AssetRegistry.load(REPO_ROOT / "library"), load_navarasa(), load_pricing(root))
        print(f"shot pack written to {pack}/SHOTPACK.md; save clips into {shotpack.inbox_dir(root, 'video')}")
    else:
        st = shotpack.status(root)
        total = len(st["done"]) + len(st["missing"])
        print(f"{len(st['done'])}/{total} shots have clips ({st['attempts']} attempts in inbox/video)")
        if st["missing"]:
            print("missing: " + ", ".join(st["missing"]))


def cmd_approve(args) -> None:
    paths = ProjectPaths(_project(args.slug))
    paths.reviews.mkdir(parents=True, exist_ok=True)
    (paths.reviews / f"{args.gate}.pending").unlink(missing_ok=True)
    (paths.reviews / f"{args.gate}.approved").write_text("approved\n", encoding="utf-8")
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
    creds = youtube.credentials(Path(args.client_secrets), paths.root / ".youtube_token.json")
    captions = {lang: paths.master / f"subs_{lang}.srt" for lang in ("te", "en")
                if (paths.master / f"subs_{lang}.srt").exists() and not args.trailer}
    thumb = paths.master / "thumbnail.png"
    video_id = youtube.upload(video, meta, creds, captions=captions, thumbnail=thumb if thumb.exists() else None)
    print(f"uploaded as {args.privacy}: https://www.youtube.com/watch?v={video_id}")


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    except GoogleSetupError as e:
        sys.exit(f"Google setup needed: {e}")
    except GoogleAPIError as e:
        sys.exit(f"Google API error: {e}")


def _main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="nraiorbit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new")
    p.add_argument("slug")
    p.add_argument("--story")
    p.add_argument("--prompt")
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
        p.set_defaults(fn=fn)

    p = sub.add_parser("retake", help="new attempt for chosen shots (paid), or --use N to pick an earlier one (free)")
    p.add_argument("slug")
    p.add_argument("shots", nargs="+", help="shot ids, e.g. sc002_s005")
    p.add_argument("--use", type=int, help="use this existing attempt instead of generating a new one")
    p.add_argument("--max-spend", type=float)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_retake)

    p = sub.add_parser("identity", help="generate character reference images with Gemini")
    p.add_argument("action", choices=["build"])
    p.add_argument("slug")
    p.add_argument("--force", action="store_true", help="regenerate existing images")
    p.add_argument("--max-spend", type=float)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_identity)

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
    p.add_argument("gate", choices=["story", "cast", "pacing", "final_cut", "trailer"])
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
    p.set_defaults(fn=cmd_publish)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
