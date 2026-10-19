"""YouTube publishing via the YouTube Data API v3.

Uploads default to `private` so you review the film in YouTube Studio before it goes
public. The altered/synthetic-content disclosure (`containsSyntheticMedia`) is always set.

Install: pip install google-api-python-client google-auth-oauthlib
"""

from __future__ import annotations

from pathlib import Path

from ..planning.runtime import scene_start_times
from ..schemas import Outline, StoryBible

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",   # captions
]
FILM_AND_ANIMATION = "1"
CHAPTER_LABELS = {
    "hero_intro": "Hero Introduction", "interval_bang": "Interval", "flashback": "Flashback",
    "pre_climax": "Pre-Climax", "climax": "Climax", "emotional_peak": "", "fight": "", "chase": "", "song": "",
}


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def chapters(outline: Outline) -> list[tuple[float, str]]:
    """YouTube chapters: first at 00:00, at least 3, each at least 10 s long."""
    starts = scene_start_times(outline)
    marks = [(0.0, "Opening")]
    for sc in outline.scenes:
        label = CHAPTER_LABELS.get(sc.set_piece)
        if label and starts[sc.scene_id] - marks[-1][0] >= 10:
            marks.append((starts[sc.scene_id], label))
    return marks if len(marks) >= 3 else []


def build_metadata(bible: StoryBible, outline: Outline, *, privacy: str = "private",
                   extra_tags: list[str] | None = None) -> dict:
    lines = [f"{bible.title_te} | {bible.title_en}", "", bible.logline, ""]
    chaps = chapters(outline)
    if chaps:
        lines += [f"{_clock(t)} {label}" for t, label in chaps] + [""]
    lines.append("This film was made with AI-generated visuals, voices and music.")
    tags = sorted({"Telugu movie", "Telugu full movie", bible.title_en, *bible.genres, *(extra_tags or [])})
    return {
        "snippet": {
            "title": f"{bible.title_te} ({bible.title_en}) | Telugu Full Movie"[:100],
            "description": "\n".join(lines)[:5000],
            "tags": tags,
            "categoryId": FILM_AND_ANIMATION,
            "defaultLanguage": "te",
            "defaultAudioLanguage": "te",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }


TOKEN_SECRET = "YOUTUBE_TOKEN_JSON"


def stored_token() -> str | None:
    """The channel's token as the control room stored it: the YOUTUBE_TOKEN_JSON environment variable, else the
    Secret Manager secret of that name (what "Connect YouTube" writes)."""
    import os

    seeded = os.environ.get(TOKEN_SECRET, "").strip()
    if seeded:
        return seeded
    from ..ops import secrets
    return (secrets.read(TOKEN_SECRET) or "").strip() or None


def token_available(token_cache: Path) -> bool:
    return token_cache.exists() or stored_token() is not None


def credentials(client_secrets: Path, token_cache: Path, interactive: bool = True):
    """The stored OAuth token (refreshed when needed). With interactive=False, as on a server, a missing or dead
    token is an error with the fix in it instead of a browser window. A token stored by the control room
    (YOUTUBE_TOKEN_JSON in the environment or Secret Manager) seeds the token file when there is none."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    seeded = stored_token() if not token_cache.exists() else None
    if seeded:
        token_cache.parent.mkdir(parents=True, exist_ok=True)
        token_cache.write_text(seeded, encoding="utf-8")
    creds = Credentials.from_authorized_user_file(str(token_cache), SCOPES) if token_cache.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_cache.write_text(creds.to_json(), encoding="utf-8")
    if creds and creds.valid:
        return creds
    if not interactive:
        raise RuntimeError(f"no valid YouTube token at {token_cache}: press Connect YouTube in the control room, or "
                           "run `nraiorbit release youtube <slug> --auth` once on a computer with a browser")
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES).run_local_server(port=0)
    token_cache.write_text(creds.to_json(), encoding="utf-8")
    return creds


def upload(video: Path, metadata: dict, creds, *, captions: dict[str, Path] | None = None,
           thumbnail: Path | None = None) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    yt = build("youtube", "v3", credentials=creds)
    request = yt.videos().insert(part="snippet,status", body=metadata,
                                 media_body=MediaFileUpload(str(video), chunksize=-1, resumable=True))
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response["id"]
    names = {"te": "Telugu", "en": "English"}
    for lang, srt in (captions or {}).items():
        yt.captions().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "language": lang, "name": names.get(lang, lang)}},
            media_body=MediaFileUpload(str(srt)),
        ).execute()
    if thumbnail:
        yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail))).execute()
    return video_id


def channel(creds) -> dict | None:
    """The signed-in account's channel: {"id", "title"}; None when the account has no channel."""
    from googleapiclient.discovery import build

    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items") or []
    if not items:
        return None
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}

