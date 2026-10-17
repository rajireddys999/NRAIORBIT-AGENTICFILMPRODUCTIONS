"""Deliverables of a finished film (`nraiorbit release deliver <slug>`): the 1080p master, a 720p copy, the trailer,
optionally chat-sized parts, uploaded to the bucket's final/ folder under dated names, with 7-day download links.
The master is never re-encoded; the 720p copy and the parts are made from it. Nothing here is billable beyond
storage pennies.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path

from ..post import ffmpeg
from . import gcs

SMALL = ["-vf", "scale=1280:-2:flags=lanczos", "-c:v", "libx264", "-preset", "medium", "-b:v", "2000k",
         "-maxrate", "2400k", "-bufsize", "4800k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart"]


def names(title: str, date: str, tag: str = "") -> dict[str, str]:
    """Remote file names: <Title>_<date>[_tag]_1080p.mp4, _720p.mp4 and the trailer."""
    base = "_".join(x for x in (title.replace(" ", "_"), date, tag) if x)
    return {"master": f"{base}_1080p.mp4", "small": f"{base}_720p.mp4",
            "trailer": f"{title.replace(' ', '_')}_trailer_{date}{'_' + tag if tag else ''}.mp4"}


def encode_720p(master: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-i", str(master), *SMALL, str(out)])
    return out


def split_parts(master: Path, out_dir: Path, parts: int, stem: str = "part") -> list[tuple[Path, float, float]]:
    """`parts` pieces of the master at 720p, re-encoded so every cut is frame-exact; (path, start_s, length_s)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total = ffmpeg.duration(master)
    step = total / parts
    made = []
    for k in range(parts):
        start, length = k * step, min(step, total - k * step)
        dest = out_dir / f"{stem}{k + 1}of{parts}.mp4"
        ffmpeg.run(["-ss", f"{start:.3f}", "-i", str(master), "-t", f"{length:.3f}", *SMALL, str(dest)])
        made.append((dest, round(start, 3), round(length, 3)))
    return made


def title_of(project_root: Path) -> str:
    bible = project_root / "01_story_bible.json"
    if bible.exists():
        try:
            return json.loads(bible.read_text(encoding="utf-8")).get("title_en") or project_root.name
        except (ValueError, AttributeError):
            pass
    return project_root.name


def deliver(project_root: Path, bucket: str | None = None, *, prefix: str = "final", date: str | None = None,
            tag: str = "", days: int = 7, parts: int = 0, upload: bool = True, log=print) -> list[dict]:
    """Make the deliverables and (unless upload=False) put them in gs://<bucket>/<slug>/<prefix>/ with signed links.
    Returns one record per file: local path, remote name, size, url."""
    project_root = Path(project_root)
    master = project_root / "11_master" / "movie.mp4"
    trailer = project_root / "11_master" / "trailer.mp4"
    if not master.exists():
        raise FileNotFoundError(f"{master} is missing: run the pipeline to the assemble stage first")
    date = date or datetime.date.today().isoformat()
    nm = names(title_of(project_root), date, tag)
    out_dir = project_root / "11_master" / "deliver"
    out_dir.mkdir(parents=True, exist_ok=True)
    small = out_dir / nm["small"]
    if not small.exists():
        log(f"  720p copy -> {small.name}")
        encode_720p(master, small)
    files = [(master, nm["master"]), (small, nm["small"])]
    if trailer.exists():
        files.append((trailer, nm["trailer"]))
    for dest, start, length in (split_parts(master, out_dir / "parts", parts, stem=f"{nm['master'][:-10]}_part")
                                if parts else []):
        files.append((dest, f"parts/{dest.name}"))
    records = []
    slug = project_root.name
    bucket = bucket if (bucket or not upload) else gcs.default_bucket()
    for local, name in files:
        rec = {"local": str(local), "name": name, "mb": round(local.stat().st_size / 1e6, 1)}
        if upload:
            remote = f"{slug}/{prefix}/{name}"
            gcs.upload(local, bucket, remote, content_type="video/mp4")
            rec["gs"] = f"gs://{bucket}/{remote}"
            rec["url"] = gcs.signed_url(bucket, remote, days=days, filename=name)
            log(f"  {name} ({rec['mb']} MB): {rec['url']}")
        records.append(rec)
    (out_dir / "delivery.json").write_text(json.dumps({"date": date, "files": records}, indent=1), encoding="utf-8")
    return records


def probe_width(path: Path) -> int:
    return int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                               "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip() or 0)
