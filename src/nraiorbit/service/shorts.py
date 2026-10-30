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
MODE_SUFFIX = ".shorts-mode.json"        # stories/<slug>.shorts-mode.json: the owner's choices for one film (how
MODE_FILE = "shorts_mode.json"           #   they go up, how many); the copy the tick puts in the project for the run
DAILY_LIMIT = ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "exceeded the number of videos")
RESET_UTC_HOUR = 8                       # YouTube's daily quota starts again at midnight Pacific time (07:00 or 08:00
#                                          UTC); the rest of the Shorts go up after this hour


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


def _uploaded(record) -> dict[str, dict]:
    """Every Short the record holds, by the content hash of the file that went up."""
    record = record if isinstance(record, dict) else {}
    return {v["file"]["sha256"]: v for k, v in record.items() if k.startswith("reel_") and isinstance(v, dict)
            and v.get("id") and isinstance(v.get("file"), dict) and v["file"].get("sha256")}


def current(record, made) -> dict[int, dict]:
    """These very reels' uploads ({reel number: entry}), matched by the file that went up: a re-cut or re-numbered
    reel set never borrows another reel's Short. Reels cut before their fingerprints were kept (no "fp") match by
    number within their cut, as they used to."""
    record = record if isinstance(record, dict) else {}
    made = made if isinstance(made, dict) else {}
    by_file, mark, out = _uploaded(record), made.get("film"), {}
    for n, r in enumerate(made.get("reels") or [], 1):
        sha = ((r.get("fp") if isinstance(r, dict) else None) or {}).get("sha256")
        if sha:
            if sha in by_file:
                out[n] = by_file[sha]
            continue
        v = record.get(f"reel_{n}")
        v = v if isinstance(v, dict) and v.get("reel") in (None, n) else None
        v = v or next((e for e in by_file.values() if e.get("reel") == n and e.get("film") == mark), None)
        if isinstance(v, dict) and v.get("id") and mark and v.get("film") == mark:
            out[n] = v
    return out


def _choices(root: Path) -> dict:
    try:
        data = json.loads((Path(root) / MODE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def count(root: Path, cfg: dict) -> int:
    """How many reels the film gets: the owner's number for this film (from the control room), else the config's
    reels.count; within 1 and post.reels.MAX_COUNT."""
    from ..post.reels import MAX_COUNT, REEL_COUNT
    for n in (_choices(root).get("count"), (cfg.get("reels") or {}).get("count"), REEL_COUNT):
        try:
            return max(1, min(MAX_COUNT, int(n)))
        except (TypeError, ValueError):
            continue
    return REEL_COUNT


def after_daily_reset(now: datetime.datetime | None = None) -> str:
    """When the rest of the Shorts may go up, once YouTube's daily upload limit was reached: the next RESET_UTC_HOUR
    (UTC), at least an hour away."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    at = now.replace(hour=RESET_UTC_HOUR, minute=10, second=0, microsecond=0)
    while at < now + datetime.timedelta(hours=1):
        at += datetime.timedelta(days=1)
    return at.isoformat(timespec="seconds")


def mode(root: Path, cfg: dict) -> str:
    """How a film's reels go up as Shorts: the owner's choice for this film (shorts_mode.json, from the control
    room), else the config's reels.shorts, else automatic."""
    chosen = _choices(root).get("mode")
    if chosen in MODES:
        return chosen
    default = (cfg.get("reels") or {}).get("shorts")
    return default if default in MODES else "auto"


def request(bucket, slug: str, privacy: str, by: str, film: str | None = None, not_before: str | None = None) -> None:
    """Ask for a film's Shorts, as the control room's button does (stories/<slug>.shorts.json); `film`: the cut the
    reels must be of (their reels.json mark), or any; `not_before`: no tick takes it earlier (YouTube's daily limit)."""
    bucket.blob(f"{REQUESTS}{slug}{SUFFIX}").upload_from_string(
        json.dumps({"id": f"{_now()}-auto", "by": by, "at": _now(), "privacy": privacy, "film": film,
                    "not_before": not_before}), content_type="application/json")


def _parsed(text) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def due(ask: dict, now: datetime.datetime | None = None) -> bool:
    """Whether a request may be taken now (one waiting for YouTube's daily limit to reset is not, until then)."""
    try:
        at = datetime.datetime.fromisoformat(str(ask.get("not_before")))
    except (ValueError, TypeError):
        return True
    at = at if at.tzinfo else at.replace(tzinfo=datetime.timezone.utc)
    return (now or datetime.datetime.now(datetime.timezone.utc)) >= at


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
    if len(current(record, made)) >= len(made["reels"]):
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
    from ..service.produce import fingerprint
    privacy = ask.get("privacy") if ask.get("privacy") in PRIVACY else "private"

    def read(name: str):
        blob = bucket.get_blob(name)
        return json.loads(blob.download_as_text()) if blob is not None else None

    record_name = f"{slug}/reports/{RECORD}"
    record = read(record_name) or {}
    record = record if isinstance(record, dict) else {}
    record.update(asked={k: ask.get(k) for k in ("by", "at")}, privacy=privacy)
    record.pop("error", None)
    record.pop("resume_after", None)

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
        up = _uploaded(record)                            # each Short by the file that went up: never twice
        with tempfile.TemporaryDirectory() as work:
            for n, reel in enumerate(reels, 1):
                local = Path(work) / reel["file"]
                from ..release import gcs
                gcs.download_blob(bucket.blob(f"{slug}/11_master/reels/{reel['file']}"), local)
                fp = fingerprint(local)
                if fp["sha256"] in up:
                    continue
                video_id = upload(local, metadata(bible, reel, privacy, film_id), creds)
                up[fp["sha256"]] = record[f"reel_{fp['sha256'][:16]}"] = {
                    "id": video_id, "file": fp, "at": _now(), "film": mark, "reel": n}
                save()                                    # each upload is recorded before the next one starts
                log(f"  Short {n} of {slug}: youtu.be/{video_id} ({privacy})")
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        if any(k in str(exc) for k in DAILY_LIMIT):         # the rest go up on their own once the limit resets
            when = after_daily_reset()
            record["error"] = "YouTube's daily upload limit was reached; the rest go up on their own once it resets"
            record["resume_after"] = when
            try:
                request(bucket, slug, privacy, "automatic, after YouTube's daily limit", film=ask.get("film"),
                        not_before=when)
            except Exception as again:                    # nothing waits: the button asks again
                record.pop("resume_after", None)
                record["error"] += f"; it could not be asked for again ({type(again).__name__}): press the button"
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
            waiting = bucket.get_blob(f"{REQUESTS}{slug}{SUFFIX}")
            if waiting is not None and not due(_parsed(waiting.download_as_text())):
                continue                                  # waiting for YouTube's daily limit to reset
            ask = take(bucket, slug)
            if ask is None:
                continue
            upload_film_shorts(bucket, slug, ask, log=log, upload=upload, creds=creds)
            done.append(slug)
        except Exception as exc:                          # one film's unreadable record never holds up the rest
            log(f"  Shorts of {slug} not looked at: {type(exc).__name__}: {str(exc)[:120]}")
    return done
