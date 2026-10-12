"""`nraiorbit` command line.

  nraiorbit new <slug> --story examples/story_godavari.md   create a project from a plot file
  nraiorbit run <slug> [--until STAGE] [--force STAGE]      run (or resume) the pipeline with Claude
  nraiorbit demo <slug>                                     offline demo: recorded agents + animatic render
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
from .library.registry import AssetRegistry
from .pipeline import STAGES, GatePending, Pipeline, PipelineBlocked, ProjectPaths

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
    except PipelineBlocked as b:
        sys.exit(f"\nBlocked: {b}")
    print(f"\nDone. Outputs in {pipeline.p.master}")


def cmd_run(args) -> None:
    from .llm import ClaudeClient

    root = _project(args.slug)
    cfg = load_pipeline_config(root)
    llm = ClaudeClient(model=cfg["llm"]["model"], max_tokens=cfg["llm"]["max_tokens"],
                       usage_log=root / "reports" / "llm_usage.jsonl")
    _run(Pipeline(root, llm, cfg=cfg), args.slug, args.until, args.force)


def cmd_demo(args) -> None:
    from .demo import DEMO_CONFIG, DEMO_PLOT, demo_llm

    root = PROJECTS / args.slug
    paths = ProjectPaths(root)
    paths.story_input.parent.mkdir(parents=True, exist_ok=True)
    if not paths.story_input.exists():
        paths.story_input.write_text(DEMO_PLOT, encoding="utf-8")
        (root / "pipeline.yaml").write_text(yaml.safe_dump(DEMO_CONFIG, sort_keys=False), encoding="utf-8")
    _run(Pipeline(root, demo_llm()), args.slug, args.until, args.force)


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
        p.set_defaults(fn=fn)

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
