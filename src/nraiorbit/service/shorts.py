"""YouTube Shorts: a film's reels (post/reels.py) uploaded when the owner asks for them in the control room, apart
from the film's own upload and its approval.

The control room's button writes stories/<slug>.shorts.json ({id, by, at, privacy}) and starts a tick. The tick
(queue.tick, before its story step) takes each request by deleting it under its generation, so two ticks never upload
the same Shorts, reads the film's reels from the bucket and uploads each one that has not gone up yet (the same file
is never uploaded twice), sending <slug>/reports/youtube_shorts.json to the bucket after every upload. Nothing here
re-renders or pays for anything; a failure is written into that record for the page to show, and the owner asks again.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

REQUESTS = "stories/"
SUFFIX = ".shorts.json"
RECORD = "youtube_shorts.json"           # <slug>/reports/
PRIVACY = ("private", "unlisted", "public")
BUSY_S = 30 * 60                         # an upload marked longer ago than this was killed: asking again is allowed
MODES = ("auto", "ask")                  # auto: right after the film's own YouTube upload; ask: only on the button
MODE_SUFFIX = ".shorts-mode.json"        # stories/<slug>.shorts-mode.json: the owner's choice for one film
MODE_FILE = "shorts_mode.json"           # the copy the tick puts in the project for the run


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def uploading(record) -> bool:
    """Whether the record says an upload of these Shorts is under way (started less than BUSY_S ago)."""
    at = (record or {}).get("uploading") if isinstance(record, dict) else None
    try:
        t = datetime.datetime.fromisoformat(str((at or {}).get("at")))
    except (ValueError, TypeError, AttributeError):
        return False
    t = t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() < BUSY_S


def current(record, mark) -> dict[str, dict]:
    """The record's uploads of this cut's reels ({reel_N: entry}); a re-cut film's old Shorts are not its own."""
    record = record if isinstance(record, dict) else {}
    return {k: v for k, v in record.items() if k.startswith("reel_") and isinstance(v, dict) and v.get("id")
            and mark and v.get("film") == mark}


def mode(root: Path, cfg: dict) -> str:
    """How a film's reels go up as Shorts: the owner's choice for this film (shorts_mode.json, from the control
    room), else the config's reels.shorts, else automatic."""
    try:
        chosen = json.loads((Path(root) / MODE_FILE).read_text(encoding="utf-8")).get("mode")
    except (OSError, ValueError, AttributeError):
        chosen = None
    if chosen in MODES:
        return chosen
    default = (cfg.get("reels") or {}).get("shorts")
    return default if default in MODES else "auto"


def request(bucket, slug: str, privacy: str, by: str, film: str | None = None) -> None:
    """Ask for a film's Shorts, as the control room's button does (stories/<slug>.shorts.json); `film`: the cut the
    reels must be of (their reels.json mark), or any."""
    bucket.blob(f"{REQUESTS}{slug}{SUFFIX}").upload_from_string(
        json.dumps({"id": f"{_now()}-auto", "by": by, "at": _now(), "privacy": privacy, "film": film}),
        content_type="application/json")


def film_privacy(youtube_record) -> str:
    """The visibility the film itself went up with (reports/youtube.json): its Shorts never go wider. A record from
    before that was written down says private."""
    film = (youtube_record or {}).get("film") if isinstance(youtube_record, dict) else None
    privacy = film.get("privacy") if isinstance(film, dict) else None
    return privacy if privacy in PRIVACY else "private"


def after_film_upload(root: Path, bucket_name: str | None, cfg: dict) -> str | None:
    """Right after the film's own YouTube upload (under the owner's approval of it), its reels are asked for as
    Shorts with the visibility the film went up with, when the film's Shorts are automatic, its reels are of this
    very cut and some are not up yet. Instagram stays the owner's: they download the reels. Returns what happened,
    for the run report (None: nothing to say)."""
    from ..post.reels import film_mark
    if not bucket_name or mode(root, cfg) != "auto":
        return None
    try:
        made = json.loads((Path(root) / "11_master" / "reels" / "reels.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not made.get("reels"):
        return None
    movie = Path(root) / "11_master" / "movie.mp4"
    if not movie.exists() or made.get("film") != film_mark(movie):
        return "not asked for: the reels are of an earlier cut"
    try:
        privacy = film_privacy(json.loads((Path(root) / "reports" / "youtube.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        privacy = "private"
    try:
        record = json.loads((Path(root) / "reports" / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    if len(current(record, made.get("film"))) >= len(made["reels"]):
        return "all up already"
    from ..release import gcs
    request(gcs.client().bucket(bucket_name), Path(root).name, privacy, "automatic, after the film's upload",
            film=made.get("film"))
    return f"asked for automatically ({privacy}); they go up when this run ends"


def requested(bucket) -> list[str]:
    """The films whose Shorts the owner asked for. `bucket`: a storage client's bucket."""
    return sorted(b.name[len(REQUESTS):-len(SUFFIX)] for b in bucket.list_blobs(prefix=REQUESTS)
                  if b.name.endswith(SUFFIX) and "/" not in b.name[len(REQUESTS):])


def take(bucket, slug: str) -> dict | None:
    """The request, removed from the bucket by this tick alone (a delete under the generation it read); None when
    another tick took it first or it is not readable."""
    from google.api_core.exceptions import NotFound, PreconditionFailed
    blob = bucket.get_blob(f"{REQUESTS}{slug}{SUFFIX}")
    if blob is None:
        return None
    try:
        ask = json.loads(blob.download_as_text())
    except ValueError:
        ask = {}
    try:
        blob.delete(if_generation_match=blob.generation)
    except (NotFound, PreconditionFailed):
        return None
    return ask if isinstance(ask, dict) else {}


def metadata(bible, reel: dict, privacy: str, film_id: str | None = None) -> dict:
    """A Short's title and description: the film's name, the line it holds, a link to the full film once that is
    up, the AI-made note; synthetic media declared, not made for kids."""
    from ..publish.youtube import FILM_AND_ANIMATION
    from ..post.reels import AI_TAG, HASHTAGS
    said = reel.get("line_en") or bible.title_en
    head = f"{bible.title_te} | {said}"
    lines = [f"{bible.title_te} | {bible.title_en}", ""]
    if reel.get("line_te") or reel.get("line_en"):
        lines += [x for x in (reel.get("line_te"), reel.get("line_en")) if x] + [""]
    lines += [bible.logline, ""]
    if film_id:
        lines += [f"Full film: https://youtu.be/{film_id}", ""]
    lines += ["This film was made with AI-generated visuals, voices and music.", AI_TAG, " ".join(HASHTAGS)]
    return {
        "snippet": {
            "title": f"{head[:91]} #Shorts",                # the tag is kept whatever the line's length
            "description": "\n".join(lines)[:5000],
            "tags": sorted({"Shorts", "Telugu", bible.title_en, *bible.genres}),
            "categoryId": FILM_AND_ANIMATION,
            "defaultLanguage": "te",
            "defaultAudioLanguage": "te",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }


def upload_film_shorts(bucket, slug: str, ask: dict, log: Callable[[str], None] = print, upload=None,
                       creds=None) -> dict:
    """Upload the film's reels that have not gone up yet; returns the record (also in the bucket after each one).
    `upload(video, metadata, creds)` and `creds` default to the YouTube client; tests pass fakes."""
    from ..schemas import StoryBible
    from ..service.produce import fingerprint, uploaded_already
    privacy = ask.get("privacy") if ask.get("privacy") in PRIVACY else "private"

    def read(name: str):
        blob = bucket.get_blob(name)
        return json.loads(blob.download_as_text()) if blob is not None else None

    record_name = f"{slug}/reports/{RECORD}"
    record = read(record_name) or {}
    record = record if isinstance(record, dict) else {}
    record.update(asked={k: ask.get(k) for k in ("by", "at")}, privacy=privacy)
    record.pop("error", None)

    def save():
        bucket.blob(record_name).upload_from_string(json.dumps(record, indent=1, ensure_ascii=False),
                                                    content_type="application/json")
    record["uploading"] = {"at": _now()}                 # the page shows it under way; no second request is taken
    save()
    try:
        made = read(f"{slug}/11_master/reels/reels.json") or {}
        reels, mark = made.get("reels") or [], made.get("film")
        if ask.get("film") and ask["film"] != mark:
            raise ValueError("the reels in storage are of another cut than the one asked for; ask again")
        if not reels:
            raise FileNotFoundError("this film has no reels yet; they are made after its trailer is approved")
        bible = StoryBible.model_validate(read(f"{slug}/project/01_story_bible.json"))
        film = (read(f"{slug}/reports/youtube.json") or {}).get("film")
        film_id = film.get("id") if isinstance(film, dict) else None
        if upload is None or creds is None:
            from ..publish import youtube
            secrets = Path(os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secret.json"))
            work_token = Path(tempfile.mkdtemp()) / ".youtube_token.json"
            token = Path(os.environ.get("YOUTUBE_TOKEN", str(work_token)))
            if not secrets.exists() and not youtube.token_available(token):
                raise FileNotFoundError("no YouTube token: press Connect YouTube in the control room")
            creds = creds or youtube.credentials(secrets, token, interactive=False)
            upload = upload or (lambda video, meta, c: youtube.upload(video, meta, c))
        with tempfile.TemporaryDirectory() as work:
            for n, reel in enumerate(reels, 1):
                local = Path(work) / reel["file"]
                from ..release import gcs
                gcs.download_blob(bucket.blob(f"{slug}/11_master/reels/{reel['file']}"), local)
                key = f"reel_{n}"
                if uploaded_already(record, key, local):
                    record[key]["film"] = mark                # the same file in a new cut is still this one
                    continue
                video_id = upload(local, metadata(bible, reel, privacy, film_id), creds)
                record[key] = {"id": video_id, "file": fingerprint(local), "at": _now(), "film": mark}
                save()                                    # each upload is recorded before the next one starts
                log(f"  Short {n} of {slug}: youtu.be/{video_id} ({privacy})")
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        log(f"  Shorts of {slug} stopped: {record['error']}")
    record.pop("uploading", None)
    save()
    return record


def run(bucket, rows: Callable[[], list[dict]], log: Callable[[str], None] = print, upload=None,
        creds=None) -> list[str]:
    """Every Shorts request whose film no run is working on (its reels may be cut again): taken, then uploaded.
    `rows()` reads the queue sheet, only when there is a request. Returns the films whose Shorts were handled."""
    asked = requested(bucket)
    if not asked:
        return []
    busy = {r["slug"] for r in rows() if str(r.get("state", "")).startswith(("producing", "approved", "retry", "new"))}
    done = []
    for slug in asked:
        if slug in busy:
            continue                                      # the next tick, once the run has written its reels
        try:
            under_way = bucket.get_blob(f"{slug}/reports/{RECORD}")
            if under_way is not None and uploading(json.loads(under_way.download_as_text() or "{}")):
                continue                                  # another tick is uploading them now
            ask = take(bucket, slug)
            if ask is None:
                continue
            upload_film_shorts(bucket, slug, ask, log=log, upload=upload, creds=creds)
            done.append(slug)
        except Exception as exc:                          # one film's unreadable record never holds up the rest
            log(f"  Shorts of {slug} not looked at: {type(exc).__name__}: {str(exc)[:120]}")
    return done
