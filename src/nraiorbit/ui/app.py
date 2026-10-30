"""The control room's routes. Reads through `films`, writes only the queue sheets and the stories folder."""

from __future__ import annotations

import datetime
import html as _html
import json
import os
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import looks as looks_mod
from ..service import feed as feed_mod
from ..service import queue
from . import films
from . import runner as runner_mod
from .store import Store

TEMPLATES_DIR = Path(__file__).parent / "templates"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def slugify(text: str) -> str:
    """A folder name from whatever was typed: lowercase ASCII letters, digits and dashes, 2 to 41 characters;
    "" when nothing usable is left (a Telugu-only title, for one)."""
    import unicodedata
    plain = re.sub(r"[\u0300-\u036f]", "", unicodedata.normalize("NFKD", text or "")).lower()
    plain = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:41].strip("-")     # same steps as the page's preview
    return plain if len(plain) >= 2 else ""


RESERVED_SLUGS = {"queue", "library", "lipsync", "stories", "settings", "final", "cache", "reports", "projects",
                  "_template", "template", "new", "film", "login", "logout", "auth", "youtube", "references", "file",
                  "healthz", "privacy", "terms", "tick"}      # bucket prefixes the runner uses, and this app's routes


def money_text(cap: float) -> str:
    """Dollars for the sheet, exact to the cent: 150 -> "150", 99999.99 -> "99999.99"."""
    return f"{cap:.2f}".rstrip("0").rstrip(".")


def pick_slug(wanted: str, story: str, taken) -> str:
    """The slug for a new film: from the name typed, else the story's first words, else the date; never a reserved
    name; unique among `taken` (a callable: slug -> bool) by a -2, -3 suffix."""
    import datetime
    first_line = next((ln for ln in story.splitlines() if ln.strip()), "")
    base = slugify(wanted) or slugify(" ".join(first_line.split()[:6])) or \
        f"film-{datetime.datetime.now(datetime.timezone.utc):%Y%m%d}"
    slug, n = base, 2
    while slug in RESERVED_SLUGS or taken(slug):
        tail = f"-{n}"
        slug, n = base[:41 - len(tail)].rstrip("-") + tail, n + 1
    return slug
STORIES = "queue/stories.csv"
REFERENCES = "queue/references.csv"


@dataclass
class Settings:
    allowed_emails: set[str] = field(default_factory=set)
    auth: bool = True
    project: str | None = None
    region: str = "us-central1"
    job: str | None = "nraiorbit-runner"
    title: str = "NRAIORBIT control room"
    google_client_id: str | None = None      # "Sign in with Google" (an OAuth web client's public id; no secret)
    google_client_secret: str | None = None  # the same client's secret: only for Connect YouTube (code exchange)
    passphrase: str | None = None            # a second way in: a long passphrase from Secret Manager (UI_PASSPHRASE)
    session_secret: str | None = None        # signs the session cookie; random per deployment when unset
    session_days: int = 7
    public_url: str | None = None            # how the browser reaches us (https://...), for OAuth redirects
    timezone: str = "America/New_York"       # the studio's clock: every time on the page, the runner log included


def public_base(request: Request, settings: "Settings") -> str:
    """The address Google must send the browser back to: the configured public URL, else the request's own host
    with the scheme the proxy in front reports (Cloud Run speaks plain HTTP to the container)."""
    if settings.public_url:
        return settings.public_url.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",")[0].strip()
    return f"{scheme}://{host}"


class Throttle:
    """Failed passphrase attempts per address: after `limit` in `window` seconds the address waits."""

    def __init__(self, limit: int = 8, window: int = 900):
        self.limit, self.window, self.hits = limit, window, {}

    def blocked(self, who: str) -> bool:
        import time
        now = time.time()
        self.hits[who] = [t for t in self.hits.get(who, []) if now - t < self.window]
        return len(self.hits[who]) >= self.limit

    def failed(self, who: str) -> None:
        import time
        self.hits.setdefault(who, []).append(time.time())


SESSION_COOKIE = "nraiorbit_session"
YOUTUBE_SETTINGS = "settings/youtube.json"        # in the store: which channel is connected, by whom, when
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def oauth_url(client_id: str, redirect_uri: str, state: str, scopes: list[str]) -> str:
    from urllib.parse import urlencode
    return GOOGLE_AUTH + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(scopes),
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state})


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """The token endpoint's answer for an authorization code: access_token, refresh_token, expires_in, scope."""
    import requests
    r = requests.post(GOOGLE_TOKEN, data={"code": code, "client_id": client_id, "client_secret": client_secret,
                                          "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed: {r.status_code} {r.text[:200]}")
    return r.json()


def authorized_user_json(tokens: dict, client_id: str, client_secret: str, scopes: list[str]) -> str:
    """The token in the shape google-auth reads back (Credentials.from_authorized_user_info)."""
    import datetime
    import json
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(tokens.get("expires_in", 3600)))
    return json.dumps({"type": "authorized_user", "token": tokens.get("access_token"),
                       "refresh_token": tokens.get("refresh_token"), "client_id": client_id,
                       "client_secret": client_secret, "token_uri": GOOGLE_TOKEN,
                       "scopes": tokens.get("scope", " ").split() or scopes,
                       "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")})


def channel_info(token_json: str) -> dict | None:
    """Which channel the token belongs to, looked up with it."""
    import json

    from google.oauth2.credentials import Credentials

    from ..publish import youtube
    creds = Credentials.from_authorized_user_info(json.loads(token_json), youtube.SCOPES)
    return youtube.channel(creds)


def store_token(token_json: str) -> bool:
    from ..ops import secrets
    from ..publish import youtube
    return secrets.add_version(youtube.TOKEN_SECRET, token_json)


def verify_google_token(token: str, client_id: str) -> dict | None:
    """The claims of a Google ID token issued to our client id, or None when it does not verify."""
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token
        claims = id_token.verify_oauth2_token(token, g_requests.Request(), client_id)
        ok = claims.get("iss") in ("accounts.google.com", "https://accounts.google.com") and claims.get("email_verified")
        return claims if ok else None
    except Exception:
        return None


def sign_session(secret: str, email: str, days: int) -> str:
    import hashlib
    import hmac
    import time
    exp = int(time.time()) + days * 86400
    body = f"{email}|{exp}"
    return body + "|" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def read_session(secret: str, cookie: str | None) -> str | None:
    import hashlib
    import hmac
    import time
    if not cookie or cookie.count("|") != 2:
        return None
    email, exp, mac = cookie.split("|")
    body = f"{email}|{exp}"
    if not hmac.compare_digest(mac, hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    return email if exp.isdigit() and int(exp) > time.time() else None


IAP_CERTS = "https://www.gstatic.com/iap/verify/public_key-jwk"


def verify_assertion(token: str) -> dict | None:
    """The claims of Identity-Aware Proxy's signed assertion, or None when it does not verify."""
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token
        claims = id_token.verify_token(token, g_requests.Request(), certs_url=IAP_CERTS)
        return claims if claims.get("iss") == "https://cloud.google.com/iap" else None
    except Exception:
        return None


def user_email(request: Request) -> str | None:
    """Who Identity-Aware Proxy signed in, read from its signed assertion only. The plain e-mail header is never
    trusted: if the service were ever opened to the internet without IAP, anyone could send that header."""
    assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
    if not assertion:
        return None
    claims = verify_assertion(assertion)
    return ((claims or {}).get("email") or "").strip().lower() or None


def denied_page(seen: str | None, allowed: set[str]) -> str:
    who = (f"You are signed in as <b>{seen}</b>." if seen else
           "No signed-in account reached this page (the request did not come through Identity-Aware Proxy).")
    return ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Private</title><body style='font:16px system-ui;max-width:40em;margin:3em auto;padding:0 1em'>"
            f"<h1>This control room is private</h1><p>{who}</p><p>Only these accounts may use it: "
            f"<b>{', '.join(sorted(allowed)) or 'none configured'}</b>.</p>"
            "<p>Sign out of Google in this browser, or open the link in a private window and sign in with the right "
            "account.</p></body>")


def load_rows(store: Store, rel: str, columns: list[str]) -> list[dict]:
    return queue.rows_of(store.read_text(rel) or "", columns)


def duplicates(executions: list[dict], doings: dict) -> set[str]:
    """Running executions whose doing records name the same film. The runner's claim keeps the second one from
    paying twice, but it still burns its time: the owner should see it and cancel it."""
    by_slug: dict[str, list[str]] = {}
    for e in executions:
        rec = doings.get(e["name"]) if e.get("state") == "running" else None
        if rec and rec.get("slug"):
            by_slug.setdefault(rec["slug"], []).append(e["name"])
    return {n for names in by_slug.values() if len(names) > 1 for n in names}


def log_time(lines: list[str], started: str | None = None, now=None, tz=datetime.timezone.utc) -> str | None:
    """The ISO moment of the last timed log line. A line carries only HH:MM:SS on the studio's clock (`tz`, as
    runner.logs stamps it), so the date comes from the
    execution's start (a line is never earlier than that; past midnight it is the next day), else from today: an
    execution three days old must read as three days old. None when no line has a time ("log unavailable")."""
    m = next((m for m in (re.match(r"^(\d\d):(\d\d):(\d\d)\b", ln or "") for ln in reversed(lines)) if m), None)
    if not m:
        return None
    hms = {"hour": int(m[1]), "minute": int(m[2]), "second": int(m[3]), "microsecond": 0}
    start = films.when(started)
    if start:
        t = start.astimezone(tz).replace(**hms)
        if t < start - datetime.timedelta(minutes=1):
            t += datetime.timedelta(days=1)
    else:
        now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
        t = now.replace(**hms)
        if t > now + datetime.timedelta(minutes=1):
            t -= datetime.timedelta(days=1)
    return t.isoformat(timespec="seconds")


SCAN_EXECUTIONS = 40          # executions to look through for one still running: a claim lasts six hours and a
                              # scheduled tick comes every 15 minutes, so a long render sits behind up to 24 quick ones


def edit_row(store: Store, slug: str, change) -> dict:
    """Change one film's row in the queue sheet under the store's write precondition, so an edit here and a tick's
    claim seconds apart never lose each other. `change(row)` edits the row in place; returning False leaves the sheet
    untouched. Returns the row as it stands afterwards."""
    seen: dict = {}

    def apply(text: str):
        rows = queue.rows_of(text, queue.STORY_COLUMNS)
        row = next((r for r in rows if r["slug"] == slug), None)
        if row is None:
            raise HTTPException(400, f"{slug} is not in the queue; films made by hand are driven from Claude Code")
        seen["row"] = row
        if change(row) is False:
            return None
        return queue.csv_of(rows, queue.STORY_COLUMNS)
    store.update_text(STORIES, apply, "text/csv")
    return seen["row"]


def create_app(store: Store, settings: Settings | None = None) -> FastAPI:
    import secrets as _secrets
    settings = settings or Settings(auth=False)
    if not settings.session_secret:
        settings.session_secret = _secrets.token_hex(32)
    app = FastAPI(title=settings.title, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = lambda v: "" if v is None else f"${float(v):,.2f}"
    templates.env.filters["short"] = lambda v, n=90: (v if len(v) <= n else v[:n - 1] + "…") if isinstance(v, str) else v
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.timezone or "UTC")
    except Exception:                                    # an unknown zone name: the page still renders, in UTC
        print(f"control room: unknown UI_TIMEZONE {settings.timezone!r}; times shown in UTC", flush=True)
        tz = datetime.timezone.utc

    def local(v, fmt: str = "%Y-%m-%d %H:%M") -> str:
        """A timestamp (ISO text from the sheets, the API or a report; UTC when it carries no zone) on the
        studio's clock, with the zone's name."""
        if not v:
            return ""
        if isinstance(v, datetime.datetime):
            d = v
        else:
            try:
                d = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                return str(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        d = d.astimezone(tz)
        return f"{d.strftime(fmt)} {d.tzname() or ''}".strip()
    templates.env.filters["local"] = local
    templates.env.filters["ago"] = films.ago
    templates.env.filters["clock"] = lambda v: films.clock(v, tz)

    throttle = Throttle()

    def guard(request: Request) -> str:
        """Who may act: a session cookie this app minted (after "Sign in with Google" or the passphrase), or
        Identity-Aware Proxy's assertion when the service sits behind IAP. Anyone else is sent to the sign-in page
        (or told no, on a form post)."""
        if not settings.auth:
            return "local"
        session = read_session(settings.session_secret, request.cookies.get(SESSION_COOKIE))
        if session:
            return session
        email = user_email(request)
        if email and email in settings.allowed_emails:
            return email
        if email:
            print(f"control room: denied {email} on {request.url.path}", flush=True)
            raise HTTPException(status_code=403, detail=denied_page(email, settings.allowed_emails))
        if request.method == "GET":
            raise HTTPException(status_code=303, headers={"Location": "/login"}, detail="sign in first")
        raise HTTPException(status_code=403, detail=denied_page(None, settings.allowed_emails))

    seen_versions: dict = {}

    def versions() -> dict:
        """What is live, for the footer: this control room's commit and the image the runner's next tick starts
        with (read from Cloud Run at most once a minute; blank when it cannot be read)."""
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        if not seen_versions or now - seen_versions.get("at", 0) > 60:
            image = runner_mod.job_image(settings.project, settings.region, settings.job)
            seen_versions.update(at=now, runner=runner_mod.short_image(image) if image else "")
        ui = os.environ.get("NRAIORBIT_VERSION") or "dev"
        return {"ui": ui[:7] if ui not in ("dev", "unknown") else ui, "runner": seen_versions.get("runner", "")}

    def page(name: str, request: Request, email: str, **kw):
        ctx = {"request": request, "settings": settings, "email": email, "root": store.root, "is_gs": store.is_gs,
               "msg": request.query_params.get("msg"), "now": datetime.datetime.now(tz),
               "versions": versions() if email else {}, **kw}
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, msg: str) -> RedirectResponse:
        from urllib.parse import quote
        return RedirectResponse(f"{url}?msg={quote(msg)}", status_code=303)

    def runner_panel() -> dict:
        panel = {"project": settings.project, "job": settings.job, "executions": [], "error": None}
        try:
            panel["executions"] = runner_mod.executions(settings.project, settings.region, settings.job,
                                                        limit=SCAN_EXECUTIONS)
        except Exception as exc:                       # no roles, no network, no job: the page still renders
            panel["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return panel

    def running_execution() -> str | None:
        """The name of a tick under way, if any: never two executions on the queue at once. One just started is
        pending for its first minute while Cloud Run schedules it, and counts too; a cancelled one has a completion
        time, so it cannot hold the queue up."""
        busy = [e for e in runner_mod.executions(settings.project, settings.region, settings.job, limit=SCAN_EXECUTIONS)
                if e["state"] == "running" or (e["state"] == "pending" and not e.get("completed"))]
        return busy[0]["name"] if busy else None

    def stopped_run(slug: str, row: dict | None, panel: dict) -> bool:
        """Whether a producing row's claim is held by no one: the run behind it stopped (killed, out of time), so the
        owner may run the film again. Never a guess, since a wrong yes pays for a render twice: the runner must be
        configured and Cloud Run must have answered; no execution may be running or pending; and the film's own
        progress record must not be from a run still under way: a record that says running frees the claim only
        when the listing shows its execution finished (a long render falls out of a short listing, which is why
        the listing looks SCAN_EXECUTIONS back; one the listing does not know at all keeps the claim)."""
        if not (settings.project and settings.job) or panel.get("error") or not row or row.get("state") != "producing":
            return False
        seen = panel["executions"]
        if any(e["state"] == "running" or (e["state"] == "pending" and not e.get("completed")) for e in seen):
            return False
        record = films.progress_record(store, slug)
        if record and record.get("state") == "running":
            ended = {e["name"] for e in seen if e["state"] in ("ok", "failed") or e.get("completed")}
            return record.get("execution") in ended
        return True

    # ------------------------------------------------------------------ sign-in
    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        """Public: Google's consent screen links here. Also the truth about what this page does with data."""
        return templates.TemplateResponse(request, "policy.html", {
            "request": request, "settings": settings, "email": None, "root": store.root, "msg": None,
            "kind": "privacy", "origin": public_base(request, settings),
            "contact": next(iter(sorted(settings.allowed_emails)), "")})

    @app.get("/terms", response_class=HTMLResponse)
    def terms(request: Request):
        return templates.TemplateResponse(request, "policy.html", {
            "request": request, "settings": settings, "email": None, "root": store.root, "msg": None,
            "kind": "terms", "origin": public_base(request, settings),
            "contact": next(iter(sorted(settings.allowed_emails)), "")})

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request):
        if not settings.auth:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "settings": settings, "email": None, "root": store.root,
            "msg": request.query_params.get("msg"), "client_id": settings.google_client_id,
            "passphrase": bool(settings.passphrase), "origin": public_base(request, settings)})

    def _session_response(identity: str, request: Request) -> RedirectResponse:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, sign_session(settings.session_secret, identity, settings.session_days),
                        max_age=settings.session_days * 86400, httponly=True,
                        secure=public_base(request, settings).startswith("https"), samesite="lax", path="/")
        print(f"control room: {identity} signed in", flush=True)
        return resp

    @app.post("/auth/passphrase")
    def auth_passphrase(request: Request, passphrase: str = Form("")):
        import hmac
        import time
        if not settings.passphrase:
            raise HTTPException(404, "passphrase sign-in is not enabled")
        who = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
        if throttle.blocked(who):
            raise HTTPException(429, "too many attempts; wait fifteen minutes")
        if not hmac.compare_digest(passphrase.strip().encode(), settings.passphrase.encode()):
            throttle.failed(who)
            print(f"control room: wrong passphrase from {who}", flush=True)
            time.sleep(1.0)
            return RedirectResponse("/login?msg=wrong+passphrase", status_code=303)
        label = next(iter(sorted(settings.allowed_emails)), "owner")
        return _session_response(f"{label} (passphrase)", request)

    @app.post("/auth/callback")
    def auth_callback(request: Request, credential: str = Form(...), g_csrf_token: str = Form("")):
        if not settings.google_client_id:
            raise HTTPException(503, "sign-in is not configured: no Google client id")
        if not g_csrf_token or g_csrf_token != request.cookies.get("g_csrf_token"):
            raise HTTPException(400, "sign-in request did not come from this page (csrf)")
        claims = verify_google_token(credential, settings.google_client_id)
        email = ((claims or {}).get("email") or "").strip().lower()
        if not claims or not email:
            raise HTTPException(401, "Google did not vouch for this sign-in")
        if email not in settings.allowed_emails:
            print(f"control room: denied {email} at sign-in", flush=True)
            raise HTTPException(status_code=403, detail=denied_page(email, settings.allowed_emails))
        return _session_response(email, request)

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login?msg=signed+out", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ------------------------------------------------------------------ Connect YouTube (the channel's upload token)
    def youtube_status() -> dict:
        import json
        text = store.read_text(YOUTUBE_SETTINGS)
        try:
            info = json.loads(text) if text else {}
        except ValueError:
            info = {}
        info["can_connect"] = bool(settings.google_client_id and settings.google_client_secret)
        return info

    def _redirect_uri(request: Request) -> str:
        return public_base(request, settings) + "/youtube/callback"

    @app.get("/youtube/connect")
    def youtube_connect(request: Request, email: str = Depends(guard)):
        import secrets as _secrets

        from ..publish import youtube
        if not (settings.google_client_id and settings.google_client_secret):
            raise HTTPException(503, "Connect YouTube needs the Google OAuth client id and secret on the service")
        state = _secrets.token_urlsafe(24)
        url = oauth_url(settings.google_client_id, _redirect_uri(request), state, youtube.SCOPES)
        resp = RedirectResponse(url, status_code=303)
        resp.set_cookie("yt_state", sign_session(settings.session_secret, state, 1), max_age=600, httponly=True,
                        secure=public_base(request, settings).startswith("https"), samesite="lax", path="/youtube")
        return resp

    @app.get("/youtube/callback")
    def youtube_callback(request: Request, email: str = Depends(guard), code: str = "", state: str = "",
                         error: str = ""):
        import datetime
        import json

        from ..publish import youtube
        if error:
            return back("/", f"YouTube was not connected: Google said {error}")
        expected = read_session(settings.session_secret, request.cookies.get("yt_state"))
        if not code or not state or state != expected:
            raise HTTPException(400, "the sign-in did not start from this page (state mismatch); try Connect again")
        tokens = exchange_code(code, settings.google_client_id, settings.google_client_secret, _redirect_uri(request))
        if not tokens.get("refresh_token"):
            return back("/", "Google returned no refresh token; remove the app's access at myaccount.google.com/permissions "
                             "and connect again")
        token_json = authorized_user_json(tokens, settings.google_client_id, settings.google_client_secret, youtube.SCOPES)
        try:
            ch = channel_info(token_json)
        except Exception as exc:
            return back("/", f"the token works but the channel could not be read: {type(exc).__name__}: {str(exc)[:120]}")
        if ch is None:
            return back("/", "that Google account has no YouTube channel; connect with the channel's account")
        if not store_token(token_json):
            return back("/", "the token could not be stored in Secret Manager (YOUTUBE_TOKEN_JSON); check the "
                             "service account's secretVersionAdder role")
        store.write_text(YOUTUBE_SETTINGS, json.dumps({
            "channel_id": ch["id"], "channel": ch["title"], "connected_by": email,
            "connected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "scopes": youtube.SCOPES}, indent=1), "application/json")
        print(f"control room: YouTube connected to channel {ch['title']} by {email}", flush=True)
        resp = back("/", f"YouTube connected: {ch['title']}")
        resp.delete_cookie("yt_state", path="/youtube")
        return resp

    @app.post("/youtube/disconnect")
    def youtube_disconnect(email: str = Depends(guard)):
        store.write_text(YOUTUBE_SETTINGS, "{}", "application/json")
        return back("/", "channel record cleared; to revoke the token itself, remove the app at "
                         "myaccount.google.com/permissions")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, email: str = Depends(guard)):
        rows = load_rows(store, STORIES, queue.STORY_COLUMNS)
        by_slug = {r["slug"]: r for r in rows}
        cards = [films.summary(store, slug, by_slug.get(slug)) for slug in films.film_slugs(store, rows)]
        refs = load_rows(store, REFERENCES, queue.REFERENCE_COLUMNS)
        panel, doings = runner_panel(), runner_mod.doings(store)
        return page("home.html", request, email, films=cards, refs=refs, runner=panel, youtube=youtube_status(),
                    doings=doings, dupes=duplicates(panel["executions"], doings))

    FORM_DEFAULTS = {"slug": "", "story": "", "budget": "150", "runtime": "10", "gates": "ask", "publish": "private",
                     "start": "1"}

    @app.get("/new", response_class=HTMLResponse)
    def new_form(request: Request, email: str = Depends(guard)):
        return page("new.html", request, email, form=dict(FORM_DEFAULTS), error=None)

    @app.post("/new")
    def new_story(request: Request, email: str = Depends(guard), slug: str = Form(""), story: str = Form(""),
                  budget: str = Form(""), runtime: str = Form("10"), gates: str = Form("ask"),
                  publish: str = Form("private"), start: str = Form("")):
        form = {"slug": slug, "story": story, "budget": budget, "runtime": runtime, "gates": gates,
                "publish": publish, "start": start}

        def again(error: str):
            resp = page("new.html", request, email, form=form, error=error)
            resp.status_code = 400
            return resp

        if len(story.strip()) < 40:
            return again("The story is too short to plan a film from. A paragraph is the least; a page is better.")
        try:
            cap = float(budget)
        except ValueError:
            return again("The budget cap must be a number.")
        if not math.isfinite(cap) or cap <= 0:
            return again("The budget cap must be a real number above zero: nothing is paid without one.")
        try:
            minutes = max(1, min(int(float(runtime or 10)), 60))
        except (ValueError, OverflowError):
            return again("Runtime must be a number of minutes.")
        if gates not in ("ask", "auto") or publish not in ("none", "private", "unlisted", "public"):
            return again("Gates must be ask or auto; YouTube none, private, unlisted or public.")
        rows = load_rows(store, STORIES, queue.STORY_COLUMNS)
        used = {r["slug"] for r in rows}
        slug = pick_slug(slug, story, lambda s: s in used or store.exists(f"{s}/project/pipeline.yaml")
                         or store.exists(f"{s}/project/01_story_bible.json") or store.exists(f"stories/{s}.md"))
        store.write_text(f"stories/{slug}.md", story.strip() + "\n", "text/markdown")
        store.write_text(f"stories/{slug}.overrides.yaml",
                         yaml.safe_dump({"film": {"runtime_minutes": minutes}}), "text/yaml")

        def add(text: str) -> str:                        # under the precondition: a tick's claim lands beside it
            rows = queue.rows_of(text, queue.STORY_COLUMNS)
            if any(r["slug"] == slug for r in rows):
                raise HTTPException(400, f"{slug} exists already; pick another slug")
            rows.append({"slug": slug, "story_uri": store.uri(f"stories/{slug}.md"), "budget_usd": money_text(cap),
                         "gates": gates, "publish": publish, "state": "new", "note": "", "updated": queue.now()})
            return queue.csv_of(rows, queue.STORY_COLUMNS)
        store.update_text(STORIES, add, "text/csv")
        msg = f"{slug} queued; the runner picks it up within 15 minutes"
        if start and settings.project and settings.job:
            try:
                ex = runner_mod.run(settings.project, settings.region, settings.job)
                msg = f"{slug} queued and a tick started ({ex})"
            except Exception as exc:
                msg = f"{slug} queued; the tick could not be started: {type(exc).__name__}"
        return back(f"/film/{slug}", msg)

    @app.get("/film/{slug}", response_class=HTMLResponse)
    def film(slug: str, request: Request, email: str = Depends(guard)):
        if not SLUG_RE.match(slug) and slug != "_template":
            raise HTTPException(404)
        rows = load_rows(store, STORIES, queue.STORY_COLUMNS)
        row = next((r for r in rows if r["slug"] == slug), None)
        if row is None and not store.exists(f"{slug}/project/pipeline.yaml") and \
                not store.exists(f"{slug}/project/01_story_bible.json"):
            raise HTTPException(404, f"no film called {slug}")
        panel, doings = runner_panel(), runner_mod.doings(store)
        running = {e["name"] for e in panel["executions"] if e["state"] == "running"}
        d = films.detail(store, slug, row, running, tz=tz)
        log_lines, latest, last_log_at = [], None, None
        if panel["executions"]:
            latest = dict(panel["executions"][0])
            try:
                log_lines = runner_mod.logs(settings.project, latest["name"], tz=tz)
            except Exception as exc:
                log_lines = [f"(log unavailable: {type(exc).__name__})"]
            last_log_at = log_time(log_lines, latest.get("created"), tz=tz)
            latest["minutes"] = int((films.seconds_since(latest.get("created")) or 0) // 60)
        stalled = None                                    # a claim with no execution behind it: the run stopped
        if stopped_run(slug, row, panel):
            hours = float(os.environ.get("NRAIORBIT_CLAIM_HOURS") or queue.CLAIM_HOURS)   # as the job's claimed() reads
            since = films.when(row.get("updated"))                                         # it, when this service carries it
            limit = since + datetime.timedelta(hours=hours) if since else None
            beat = queue.takeover_at(row, films.progress_record(store, slug))              # a run that beats: minutes
            resumes = min(t for t in (beat, limit) if t is not None) if (beat or limit) else None
            stalled = {"since": row.get("updated"), "hours": hours, "resumes": resumes.isoformat() if resumes else None}
        return page("film.html", request, email, f=d, stages=films.STAGES, runner=panel, log_lines=log_lines,
                    latest=latest, last_log_at=last_log_at, runner_running=bool(running), youtube=youtube_status(),
                    doings=doings, dupes=duplicates(panel["executions"], doings), stalled=stalled)

    # ------------------------------------------------------------------ actions (the queue sheet only)
    @app.post("/film/{slug}/approve")
    def approve(slug: str, email: str = Depends(guard), gate: str = Form("")):
        if gate not in queue.GATES:
            raise HTTPException(400, f"no gate called {gate[:40]!r}; one of {', '.join(queue.GATES)}")
        refused: dict = {}

        drawn_first = looks_waiting(slug) if gate == "cast" else []

        def change(row: dict):
            if row["state"] == "approved":                    # pressed twice: one approval, one tick
                refused["msg"] = "already approved; the runner takes it up on its next tick"
                return False
            if row["state"] != f"awaiting:{gate}":            # a stale page, or a run under way: never touch the claim
                refused["msg"] = f"nothing waits at the {gate} gate: the film is {row['state']}; nothing changed"
                return False
            if drawn_first:                                   # a look saved but not drawn: drawn, then asked again
                row.update(state="retry", note=f"the owner's looks for {', '.join(drawn_first)} are drawn first",
                           updated=queue.now())
                return None
            row.update(state="approved", note=f"{gate} approved in the control room by {email}", updated=queue.now())
        edit_row(store, slug, change)
        if refused:
            return back(f"/film/{slug}", refused["msg"])
        if drawn_first:
            return back(f"/film/{slug}", f"Your look for {', '.join(drawn_first)} is drawn first, so you see it before "
                                         "you approve: " + tick_now("the cast waits for you again once it is drawn")
                        + " (if it never arrives, choose Use the AI's own look for that character)")
        return back(f"/film/{slug}", f"{gate} approved; " + tick_now("the next step is under way"))

    def looks_waiting(slug: str) -> list[str]:
        """Characters whose saved look the cast step has not taken in yet."""
        given = films._yaml(store, f"{looks_mod.film_folder(slug)}/{looks_mod.CHOICES}")
        bible = films._json(store, f"{slug}/project/01_story_bible.json") or {}
        lock = (films._yaml(store, f"{slug}/project/cast.lock.yaml") or {}).get("characters")
        return looks_mod.waiting(given if isinstance(given, dict) else {},
                                 films._json(store, f"{slug}/reports/looks.json") or {},
                                 {c.get("id") for c in bible.get("characters") or []},
                                 lock if isinstance(lock, dict) else {},
                                 films._json(store, f"{slug}/reports/looks_waiting.json") or {})

    def look_saved(slug: str, character: str, what: str) -> RedirectResponse:
        """After a look is saved or removed: a cast waiting for the owner is drawn again at once (and asks again)."""
        row = next((r for r in load_rows(store, STORIES, queue.STORY_COLUMNS) if r["slug"] == slug), None)
        if row is not None and row["state"] == "awaiting:cast":
            def change(r: dict):
                if r["state"] != "awaiting:cast":
                    return False
                r.update(state="retry", note=f"the owner's look for {character} is being drawn", updated=queue.now())
            edit_row(store, slug, change)
            return back(f"/film/{slug}", f"{what}; " + tick_now(
                "the runner draws it now (a few cents) and the cast waits for you again"))
        return back(f"/film/{slug}", f"{what}; the runner takes it in at the cast step, before you approve the cast")

    def look_of(slug: str, character: str) -> tuple[dict, str | None]:
        """(the film's bible characters by id, why a look cannot be given now or None)."""
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        bible = films._json(store, f"{slug}/project/01_story_bible.json") or {}
        chars = {c.get("id"): c for c in bible.get("characters") or []
                 if looks_mod.CHARACTER_ID.fullmatch(str(c.get("id") or ""))}
        if character not in chars:
            return chars, f"No character {character[:40]!r} in this film; nothing changed."
        row = next((r for r in load_rows(store, STORIES, queue.STORY_COLUMNS) if r["slug"] == slug), None)
        state = films.looks_state(store, slug, row, films._config(store, slug), True)
        return chars, None if state["open"] else (state["why_closed"] or "Looks cannot be given now") + " Nothing changed."

    def picture_name(character: str, png: bytes) -> tuple[str, str]:
        import hashlib
        sha = hashlib.sha256(png).hexdigest()
        return f"{character}_{sha[:12]}.png", sha

    def into_gallery(slug: str, character: str, entries: list[dict]) -> None:
        """Add pictures to the film's gallery (<slug>/characters/gallery.yaml); one already there is kept once."""
        def change(text: str):
            data = (yaml.safe_load(text) or {}) if text else {}
            data = data if isinstance(data, dict) else {}
            mine = [e for e in (data.get(character) or []) if isinstance(e, dict)]
            have = {e.get("file") for e in mine}
            new = [e for e in entries if e["file"] not in have]
            if not new:
                return None
            data[character] = mine + new
            return yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
        store.update_text(f"{looks_mod.film_folder(slug)}/{looks_mod.GALLERY}", change, content_type="text/yaml")

    def film_characters(slug: str) -> dict:
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        bible = films._json(store, f"{slug}/project/01_story_bible.json") or {}
        return {c.get("id"): c for c in bible.get("characters") or [] if looks_mod.CHARACTER_ID.fullmatch(
            str(c.get("id") or ""))}

    @app.post("/film/{slug}/pictures")
    def add_pictures(slug: str, email: str = Depends(guard), pictures: list[UploadFile] | None = File(None),
                     character_for: list[str] = Form([]), character: str = Form(""), rights: str = Form("")):
        """Pictures into the film's character gallery, each for the character picked beside it (or one character for
        all of them). Kept for this film only; choosing one as a character's face is a separate step."""
        chars = film_characters(slug)
        if next((r for r in load_rows(store, STORIES, queue.STORY_COLUMNS) if r["slug"] == slug), None) is None:
            return back(f"/film/{slug}", "Only films in the queue keep character pictures; nothing changed.")
        files = [p for p in pictures or [] if p.filename]
        if not files:
            return back(f"/film/{slug}", "Choose one or more pictures first; nothing changed.")
        if len(files) > films.MAX_FILES:
            return back(f"/film/{slug}", f"At most {films.MAX_FILES} pictures at a time; nothing changed.")
        whose = character_for if len(character_for) == len(files) else [character] * len(files)
        if any(w not in chars for w in whose):
            return back(f"/film/{slug}", "Pick a character for every picture; nothing changed.")
        if rights not in looks_mod.RIGHTS:
            return back(f"/film/{slug}", "Say whose pictures these are (yours with the people's permission, or "
                                         "AI-made); nothing changed.")
        added: dict[str, list[dict]] = {}
        problems = []
        for f, cid in zip(files, whose):
            data = f.file.read(looks_mod.MAX_PICTURE + 1)
            if len(data) > looks_mod.MAX_PICTURE:
                problems.append(f"{f.filename[:40]}: over 15 MB")
                continue
            try:
                png = looks_mod.normalize_picture(data)
            except ValueError as exc:
                problems.append(f"{f.filename[:40]}: {str(exc).replace('; nothing changed.', '')}")
                continue
            name, sha = picture_name(cid, png)
            store.write_bytes(f"{looks_mod.film_folder(slug)}/{cid}/{name}", png, "image/png")
            added.setdefault(cid, []).append({"file": name, "sha": sha, "source": "yours", "rights": rights,
                                              "by": email, "at": queue.now()})
        for cid, entries in added.items():
            into_gallery(slug, cid, entries)
        said = "; ".join(f"{len(e)} for {chars[c].get('name_en') or c}" for c, e in added.items())
        msg = (f"Added to the gallery: {said}. Choose one as a character's final face below." if added
               else "No picture was added.")
        return back(f"/film/{slug}", msg + (" Not added: " + "; ".join(problems) if problems else ""))

    @app.post("/film/{slug}/face")
    def use_face(slug: str, email: str = Depends(guard), character: str = Form(""), pick: str = Form("")):
        """A gallery picture (or a face the AI drew for this character) as the character's final face."""
        chars, why = look_of(slug, character)
        if why:
            return back(f"/film/{slug}", why)
        entry = next((e for e in films.gallery(store, slug).get(character, []) if e.get("file") == pick), None)
        if entry is None and pick.startswith("ai:"):
            lock = (films._yaml(store, f"{slug}/project/cast.lock.yaml") or {}).get("characters") or {}
            taken = films._json(store, f"{slug}/reports/looks.json") or {}
            chosen = films._yaml(store, f"{looks_mod.film_folder(slug)}/{looks_mod.CHOICES}") or {}
            mine = chosen.get(character) if isinstance(chosen, dict) and isinstance(chosen.get(character), dict) else {}
            now = (lock.get(character) or {}).get("character_asset") or ""
            if (not mine or mine.get("removed")) and now.rsplit("/", 1)[-1].split("@")[0] == pick[3:]:
                return back(f"/film/{slug}", "That is the face in use now; nothing changed.")
            face = next((f for f in films.ai_faces(
                store, slug, character, now, ((taken.get(character) or {}).get("before") or {}).get("character_asset"),
                store.dirs("library/characters/")) if f["aid"] == pick[3:]), None)
            raw = store.read_bytes(face["rel"]) if face else None
            if not raw:
                return back(f"/film/{slug}", "That face is not one of this character's; nothing changed.")
            try:
                png = looks_mod.normalize_picture(raw)
            except ValueError as exc:
                return back(f"/film/{slug}", str(exc))
            name, sha = picture_name(character, png)
            store.write_bytes(f"{looks_mod.film_folder(slug)}/{character}/{name}", png, "image/png")
            entry = {"file": name, "sha": sha, "source": "ai", "from": face["aid"], "rights": "ai", "by": email,
                     "at": queue.now()}
            into_gallery(slug, character, [entry])
        if entry is None:
            return back(f"/film/{slug}", "That picture is not in this character's gallery; nothing changed.")
        said = {}

        def change(text: str):
            data = (yaml.safe_load(text) or {}) if text else {}
            data = data if isinstance(data, dict) else {}
            old = data.get(character) if isinstance(data.get(character), dict) else {}
            old = {} if old.get("removed") else old
            if old.get("image") == entry["file"]:
                said["msg"] = "That is already the final face; nothing changed."
                return None
            new = {k: old[k] for k in ("build", "notes") if old.get(k)}
            new.update(image=entry["file"], image_sha=entry["sha"], rights=entry.get("rights") or "ai")
            data[character] = {**new, "by": email, "at": queue.now()}
            return yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
        store.update_text(f"{looks_mod.film_folder(slug)}/{looks_mod.CHOICES}", change, content_type="text/yaml")
        if said:
            return back(f"/film/{slug}", said["msg"])
        who = chars[character].get("name_en") or character
        return look_saved(slug, character, f"{who}'s final face chosen")

    @app.post("/film/{slug}/picture-remove")
    def remove_picture(slug: str, email: str = Depends(guard), character: str = Form(""), file: str = Form("")):
        """Delete one of the owner's gallery pictures from the bucket (the page asks to confirm first). The picture
        that is a character's final face stays until another is chosen."""
        chars = film_characters(slug)
        if character not in chars or not looks_mod.PICTURE_NAME.fullmatch(file) or not any(
                e.get("file") == file for e in films.gallery(store, slug).get(character, [])):
            return back(f"/film/{slug}", "That picture is not in the gallery; nothing changed.")
        chosen = films._yaml(store, f"{looks_mod.film_folder(slug)}/{looks_mod.CHOICES}") or {}
        look = chosen.get(character) if isinstance(chosen, dict) and isinstance(chosen.get(character), dict) else {}
        if not look.get("removed") and look.get("image") == file:
            return back(f"/film/{slug}", "That is the character's final face: choose another, or use the AI's own "
                                         "look, before removing it; nothing changed.")

        gone: list[dict] = []

        def change(text: str):
            data = (yaml.safe_load(text) or {}) if text else {}
            data = data if isinstance(data, dict) else {}
            mine = [e for e in (data.get(character) or []) if isinstance(e, dict)]
            gone[:] = [e for e in mine if e.get("file") == file]
            if not gone:
                return None
            data[character] = [e for e in mine if e.get("file") != file]
            return yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
        path = f"{looks_mod.film_folder(slug)}/{character}/{file}"
        kept = store.read_bytes(path)
        store.update_text(f"{looks_mod.film_folder(slug)}/{looks_mod.GALLERY}", change, content_type="text/yaml")
        store.delete(path)
        chosen = films._yaml(store, f"{looks_mod.film_folder(slug)}/{looks_mod.CHOICES}") or {}
        look = chosen.get(character) if isinstance(chosen, dict) and isinstance(chosen.get(character), dict) else {}
        if kept and not look.get("removed") and look.get("image") == file:   # chosen as the face meanwhile: kept
            store.write_bytes(path, kept, "image/png")
            into_gallery(slug, character, gone[:1] or [{"file": file, "sha": look.get("image_sha"), "source": "yours",
                                                        "rights": look.get("rights"), "by": email, "at": queue.now()}])
            return back(f"/film/{slug}", "That picture was just chosen as the final face, so it was kept.")
        return back(f"/film/{slug}", "The picture was deleted from the bucket.")

    @app.post("/film/{slug}/look")
    def set_look(slug: str, email: str = Depends(guard), character: str = Form(""), build: str = Form(""),
                 notes: str = Form(""), notes_shown: str = Form("")):
        """The owner's body build and notes for one character (looks.py); its face is chosen in the gallery. Each
        is optional; what is not given stays as it was."""
        chars, why = look_of(slug, character)
        if why:
            return back(f"/film/{slug}", why)
        if build and build != "written" and build not in looks_mod.BUILDS:
            return back(f"/film/{slug}", f"No body build called {build[:30]!r}; nothing changed.")
        said = {}
        stuck = (films._json(store, f"{slug}/reports/looks_waiting.json") or {}).get(character) or {}

        def change(text: str):
            data = (yaml.safe_load(text) or {}) if text else {}
            data = data if isinstance(data, dict) else {}
            old = data.get(character) if isinstance(data.get(character), dict) else {}
            old = {} if old.get("removed") else old
            new = {k: old[k] for k in ("image", "image_sha", "rights", "build", "notes") if old.get(k)}
            if build == "written":
                new.pop("build", None)
            elif build:
                new["build"] = build
            if notes_shown or notes.strip():
                new["notes"] = notes.strip()[:looks_mod.NOTES_MAX]
            if not new.get("notes"):
                new.pop("notes", None)
            same = old and looks_mod.fingerprint(new) == looks_mod.fingerprint(old)
            if same and stuck.get("fp") != looks_mod.fingerprint(old):     # the same again: a try once more only
                said["msg"] = "Nothing new for this character; nothing changed."    # when it could not be taken
                return None
            if not any(new.get(k) for k in ("image", "build", "notes")):
                if not old:
                    said["msg"] = "Nothing given: choose a body build or notes; nothing changed."
                    return None
                data[character] = {"removed": True, "by": email, "at": queue.now()}
            else:
                data[character] = {**new, "by": email, "at": queue.now()}
            return yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
        store.update_text(f"{looks_mod.film_folder(slug)}/{looks_mod.CHOICES}", change, content_type="text/yaml")
        if said:
            return back(f"/film/{slug}", said["msg"])
        who = chars[character].get("name_en") or character
        return look_saved(slug, character, f"{who}'s look saved")

    @app.post("/film/{slug}/look-remove")
    def remove_look(slug: str, email: str = Depends(guard), character: str = Form("")):
        """The AI's own look for this character again: its casting as it was before the owner's look."""
        chars, why = look_of(slug, character)
        if why:
            return back(f"/film/{slug}", why)
        said = {}

        def change(text: str):
            data = (yaml.safe_load(text) or {}) if text else {}
            data = data if isinstance(data, dict) else {}
            if not isinstance(data.get(character), dict) or data[character].get("removed"):
                said["msg"] = "This character has no look of yours; nothing changed."
                return None
            data[character] = {"removed": True, "by": email, "at": queue.now()}
            return yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
        store.update_text(f"{looks_mod.film_folder(slug)}/{looks_mod.CHOICES}", change, content_type="text/yaml")
        if said:
            return back(f"/film/{slug}", said["msg"])
        who = chars[character].get("name_en") or character
        return look_saved(slug, character, f"{who} goes back to the AI's own look")

    def tick_now(then: str) -> str:
        """Start a tick unless one is running, and say what happened: no fifteen-minute wait after a decision."""
        if not (settings.project and settings.job):
            return "the next tick carries on"
        try:
            busy = running_execution()
            if busy:
                return f"a tick is running ({busy}), the next scheduled one carries on"
            ex = runner_mod.run(settings.project, settings.region, settings.job)
            return f"a tick started ({ex}) and {then}"
        except Exception as exc:
            return f"the tick could not be started ({type(exc).__name__}), the scheduled one carries on"

    @app.post("/film/{slug}/voice")
    def set_voice(slug: str, email: str = Depends(guard), character: str = Form(""), voice_id: str = Form("")):
        """The owner's ElevenLabs voice for one character, into stories/<slug>.voices.yaml: the runner puts it into
        the film's voice.cast at its next run, ahead of anything it picked."""
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        voice_id = voice_id.strip()
        if not queue.VOICE_ID.fullmatch(voice_id):
            return back(f"/film/{slug}", "That does not look like an ElevenLabs voice id (letters and digits, "
                                         "from the voice's page in ElevenLabs); nothing changed.")
        bible = films._json(store, f"{slug}/project/01_story_bible.json") or {}
        if character not in {c.get("id") for c in bible.get("characters") or []}:
            return back(f"/film/{slug}", f"No character {character[:40]!r} in this film; nothing changed.")

        def change(text: str):
            chosen = (yaml.safe_load(text or "") or {}) if text else {}
            chosen = chosen if isinstance(chosen, dict) else {}
            if chosen.get(character) == voice_id:
                return None
            chosen[character] = voice_id
            return yaml.safe_dump(chosen, sort_keys=True)
        store.update_text(f"stories/{slug}.voices.yaml", change, content_type="text/yaml")
        return back(f"/film/{slug}", f"{character}'s voice set to {voice_id}; the runner uses it from its next run. "
                                     "If this character's lines were already voiced, all of them are voiced again and "
                                     "their lip-sync redone (paid, under the cap)")

    @app.post("/film/{slug}/retry")
    def retry(slug: str, email: str = Depends(guard)):
        """Run the film again, from a blocked step or from its end: the runner checks every step, keeps the finished
        ones, redoes the missing or failed ones and asks at the gates again. Never over a run under way."""
        refused: dict = {}
        current = next((r for r in load_rows(store, STORIES, queue.STORY_COLUMNS) if r["slug"] == slug), None)
        stopped = stopped_run(slug, current, runner_panel())   # a producing row whose run stopped may be run again

        def change(row: dict):
            head = row["state"].split(":")[0]
            # a stopped run's claim is freed only if the row has not moved since the decision: a tick that claimed
            # it afresh in the seconds the listing took keeps its claim, or two runs would render the same film
            if head in films.RUNNING and not (head == "producing" and stopped
                                               and row.get("updated") == (current or {}).get("updated")):
                refused["msg"] = f"the runner is already on it ({row['state']}); nothing changed"
                return False
            row.update(state="retry", updated=queue.now())
        edit_row(store, slug, change)
        if refused:
            return back(f"/film/{slug}", refused["msg"])
        return back(f"/film/{slug}", "set to retry; " + tick_now(
            "the runner checks every step, keeps the finished ones and redoes the rest; the pipeline shows the step under way"))

    @app.post("/film/{slug}/row")
    def save_row(slug: str, email: str = Depends(guard), budget: str = Form(""), gates: str = Form(""),
                 publish: str = Form("")):
        if budget:
            try:
                cap = float(budget)
                if not math.isfinite(cap) or cap <= 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "the budget cap must be a real number above zero") from None

        def change(row: dict):
            if budget:
                row["budget_usd"] = money_text(cap)
            if gates in ("ask", "auto"):
                row["gates"] = gates
            if publish in ("none", "private", "unlisted", "public"):
                row["publish"] = publish
            if row["state"] != "producing":                   # a producing row's time names the run's claim: kept
                row["updated"] = queue.now()
        edit_row(store, slug, change)
        return back(f"/film/{slug}", "saved; the next tick writes it into the project")

    @app.post("/film/{slug}/rework")
    def rework(slug: str, email: str = Depends(guard), stage: str = Form(""), shots: str = Form(""),
               notes: str = Form("")):
        """Send the film back: the owner's notes and the step to redo from (or, at the video step, the shots to plan
        again and remake). Saved beside the story as stories/<slug>.rework.json; the next tick applies it once, before
        anything runs, and the gates ask again. Never over a run under way."""
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        steps = dict(films.REWORK_STEPS)
        if stage not in steps:
            return back(f"/film/{slug}", "Pick the step to redo; nothing changed.")
        notes = notes.strip()[:2000]
        if not notes:
            return back(f"/film/{slug}", "Write what needs fixing, so the agents know what to change; nothing changed.")
        ids = sorted(set(re.findall(r"sc\d{3}_s\d{3}", shots)))
        if ids and stage != "render":
            return back(f"/film/{slug}", f"Shot ids go with the '{steps['render']}' step; nothing changed.")
        if stage == "render" and not ids:     # remaking every clip from the same words would re-buy the film's video
            return back(f"/film/{slug}", f"Name the shots to redo for '{steps['render']}' (ids like sc002_s001 from "
                                         "the shot lists); to change the plan, send it back to Shot lists. "
                                         "Nothing changed.")
        if ids:
            known = {s.get("shot_id") for rel in store.list(f"{slug}/04_shots/") if rel.endswith(".json")
                     for s in (films._json(store, rel) or {}).get("shots") or []}
            missing = [i for i in ids if i not in known]
            if missing:
                return back(f"/film/{slug}", f"No shot {', '.join(missing)} in this film; nothing changed.")
        current = next((r for r in load_rows(store, STORIES, queue.STORY_COLUMNS) if r["slug"] == slug), None)
        if current and current["state"].split(":")[0] in films.RUNNING:   # before anything is written: a tick that
            return back(f"/film/{slug}", f"the runner is on it ({current['state']}); send it back when it stops. "
                                         "Nothing changed")             # claims the row could otherwise take it
        ask = f"stories/{slug}.rework.json"
        before = store.read_text(ask) or ""                    # an earlier Send back the tick has not taken yet
        store.write_text(ask, json.dumps({"id": f"{queue.now()}-{os.urandom(3).hex()}", "stage": stage, "shots": ids,
                                          "notes": notes, "by": email, "at": queue.now()}, ensure_ascii=False),
                         "application/json")                   # first: a tick that claims the row finds it
        refused: dict = {}

        def change(row: dict):
            head = row["state"].split(":")[0]
            if head in films.RUNNING:
                refused["msg"] = (f"the runner is on it ({row['state']}); send it back when it stops. "
                                  "Nothing changed")
                return False
            row.update(state="retry", note=f"sent back to {steps[stage]} by {email}: {notes}"[:200],
                       updated=queue.now())
        try:
            edit_row(store, slug, change)
        except HTTPException:                                  # not in the queue: nothing may be left behind
            store.write_text(ask, before, "application/json")
            raise
        if refused:
            store.write_text(ask, before, "application/json")  # as it was: never applied later by surprise
            return back(f"/film/{slug}", refused["msg"])
        what = (f"shots {', '.join(ids)} are planned again from your notes and made anew" if ids else
                f"every step from {steps[stage]} on is redone, following your notes")
        return back(f"/film/{slug}", f"Sent back: {what}; the gates ask again. " + tick_now("the rework is under way"))

    @app.post("/film/{slug}/shorts")
    def shorts(slug: str, email: str = Depends(guard), privacy: str = Form("private")):
        """Upload the film's reels to YouTube as Shorts, apart from the film's own upload and its approval: saved as
        stories/<slug>.shorts.json, which the next tick takes once (service/shorts.py) and uploads each reel not up
        yet. Nothing is re-rendered or paid for."""
        from ..service import shorts as shorts_mod
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        if privacy not in shorts_mod.PRIVACY:
            return back(f"/film/{slug}", "Pick private, unlisted or public for the Shorts; nothing changed.")
        if not (films._json(store, f"{slug}/11_master/reels/reels.json") or {}).get("reels"):
            return back(f"/film/{slug}", "This film has no reels yet: they are cut after its trailer is approved. "
                                         "Nothing changed.")
        if not youtube_status().get("channel"):
            return back(f"/film/{slug}", "Connect YouTube first; nothing changed.")
        if shorts_mod.uploading(films._json(store, f"{slug}/reports/{shorts_mod.RECORD}")):
            return back(f"/film/{slug}", "The Shorts are going up now; nothing changed.")
        store.write_text(f"{shorts_mod.REQUESTS}{slug}{shorts_mod.SUFFIX}",
                         json.dumps({"id": f"{queue.now()}-{os.urandom(3).hex()}", "by": email, "at": queue.now(),
                                     "privacy": privacy}), "application/json")
        return back(f"/film/{slug}", f"Shorts asked for ({privacy}); " + tick_now("they go up in a minute or two"))

    @app.post("/film/{slug}/shorts-mode")
    def shorts_mode(slug: str, email: str = Depends(guard), mode: str = Form("")):
        """Whether this film's reels go up as YouTube Shorts on their own, right after the film's upload (auto), or
        only when the owner presses the button (ask). Saved as stories/<slug>.shorts-mode.json; the next run uses
        it. Instagram is always the owner's: they download the reels."""
        from ..service import shorts as shorts_mod
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        if mode not in shorts_mod.MODES:
            return back(f"/film/{slug}", "Pick automatic or only when you ask; nothing changed.")
        keep = films._json(store, f"{shorts_mod.REQUESTS}{slug}{shorts_mod.MODE_SUFFIX}") or {}
        store.write_text(f"{shorts_mod.REQUESTS}{slug}{shorts_mod.MODE_SUFFIX}",
                         json.dumps({**({"count": keep["count"]} if "count" in keep else {}), "mode": mode, "by": email,
                                     "at": queue.now()}), "application/json")
        said = ("they go up on their own right after the film's upload" if mode == "auto"
                else "only when you press the button")
        uploaded = films._json(store, f"{slug}/reports/youtube.json") or {}
        made = films._json(store, f"{slug}/11_master/reels/reels.json") or {}
        record = films._json(store, f"{slug}/reports/{shorts_mod.RECORD}")
        if (mode == "auto" and isinstance(uploaded.get("film"), dict) and uploaded["film"].get("id")
                and made.get("reels") and len(shorts_mod.current(record, made)) < len(made["reels"])
                and not shorts_mod.uploading(record) and not store.exists(f"stories/{slug}{shorts_mod.SUFFIX}")):
            privacy = shorts_mod.film_privacy(uploaded)  # the film is up already: its reels follow it now
            store.write_text(f"{shorts_mod.REQUESTS}{slug}{shorts_mod.SUFFIX}",
                             json.dumps({"id": f"{queue.now()}-{os.urandom(3).hex()}", "by": email, "at": queue.now(),
                                         "privacy": privacy, "film": made.get("film")}), "application/json")
            return back(f"/film/{slug}", f"YouTube Shorts: {said}. The film is on YouTube already, so its reels "
                                         f"go up now ({privacy}); " + tick_now("they go up in a minute or two"))
        return back(f"/film/{slug}", "YouTube Shorts: " + said)

    @app.post("/film/{slug}/shorts-count")
    def shorts_count(slug: str, email: str = Depends(guard), count: str = Form("")):
        """How many shorts (reels) the film gets, for Instagram and YouTube alike: each a whole moment of 5 to 15
        seconds, cut to cut. Saved with the film's Shorts choices; a finished film's are cut again at once (free)."""
        from ..post.reels import MAX_COUNT
        from ..service import shorts as shorts_mod
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        try:
            n = int(count)
        except ValueError:
            n = 0
        if not 1 <= n <= MAX_COUNT:
            return back(f"/film/{slug}", f"Choose between 1 and {MAX_COUNT} shorts; nothing changed.")
        name = f"{shorts_mod.REQUESTS}{slug}{shorts_mod.MODE_SUFFIX}"
        keep = films._json(store, name) or {}
        row = next((r for r in load_rows(store, STORIES, queue.STORY_COLUMNS) if r["slug"] == slug), None)
        cut_for = ((films._json(store, f"{slug}/11_master/reels/reels.json") or {}).get("plan") or {}).get("count")
        if keep.get("count") == n and not (row is not None and row["state"] == "done" and cut_for != n):
            return back(f"/film/{slug}", f"The film already gets {n} shorts; nothing changed.")
        store.write_text(name, json.dumps({**{k: v for k, v in keep.items() if k == "mode"}, "count": n, "by": email,
                                           "at": queue.now()}), "application/json")
        if row is not None and row["state"] == "done":   # the film is finished: its shorts are cut again now
            def change(r: dict):
                if r["state"] != "done":
                    return False
                r.update(state="retry", note=f"{n} shorts asked for: cut again from the finished film",
                         updated=queue.now())
            edit_row(store, slug, change)
            return back(f"/film/{slug}", f"{n} shorts; " + tick_now(
                "the runner cuts them from the finished film (nothing paid) and keeps everything else as it is"))
        return back(f"/film/{slug}", f"{n} shorts; they are cut when the film's shorts are made (right after the "
                                     "trailer is approved), or again on the film's next run")

    @app.post("/film/{slug}/reject")
    def reject(slug: str, email: str = Depends(guard), gate: str = Form(""), notes: str = Form("")):
        """Reject the cut or the trailer with a note: the runner's AI editor (service/triage) decides what to redo,
        the narrowest fix first, and redoes it; the page shows its decision. Saved beside the story as
        stories/<slug>.rework.json (mode "triage"); the next tick applies it once, before anything runs. Spending
        still stops at the cost and faces checks. Never over a run under way, nor on a page the film has moved on
        from."""
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        notes = notes.strip()[:2000]
        if not notes:
            return back(f"/film/{slug}", "Write what is wrong and what should change; nothing changed.")
        if films.rejects_today(store, slug) >= films.REJECTS_PER_DAY:
            return back(f"/film/{slug}", f"That is {films.REJECTS_PER_DAY} rejections today, the most in a day; "
                                         "send it back to a step instead, or reject again tomorrow. Nothing changed.")

        def refusal(state: str) -> str:
            at = state[len("awaiting:"):] if state.startswith("awaiting:") else state
            if state.split(":")[0] in films.RUNNING:
                return f"the runner is on it ({state}); reject it when it stops. Nothing changed"
            if at not in (*films.REJECT_GATES, "done") or at != gate:
                return f"the film is {state} now, not at the review this page showed; look again. Nothing changed"
            return ""
        current = next((r for r in load_rows(store, STORIES, queue.STORY_COLUMNS) if r["slug"] == slug), None)
        if current is None:
            raise HTTPException(400, f"{slug} is not in the queue; films made by hand are driven from Claude Code")
        if refusal(current["state"]):                         # before anything is written: a tick that claims the
            return back(f"/film/{slug}", refusal(current["state"]))   # row could otherwise take it
        ask = f"stories/{slug}.rework.json"
        before = store.read_text(ask) or ""                    # an earlier request the tick has not taken yet
        store.write_text(ask, json.dumps({"id": f"{queue.now()}-{os.urandom(3).hex()}", "mode": "triage", "gate": gate,
                                          "notes": notes, "by": email, "at": queue.now()}, ensure_ascii=False),
                         "application/json")                   # first: a tick that claims the row finds it
        refused: dict = {}

        def change(row: dict):
            why = refusal(row["state"])                       # again under the write: the row may have moved since
            if why:
                refused["msg"] = why
                return False
            row.update(state="retry", note=f"rejected at {gate} by {email}: {notes}"[:200], updated=queue.now())
        try:
            edit_row(store, slug, change)
        except HTTPException:                                  # not in the queue: nothing may be left behind
            store.write_text(ask, before, "application/json")
            raise
        if refused:
            store.write_text(ask, before, "application/json")  # as it was: never applied later by surprise
            return back(f"/film/{slug}", refused["msg"])
        return back(f"/film/{slug}", "Rejected: the AI editor reads your note and decides what to redo; its decision "
                                     "shows on this page. " + tick_now("the decision is under way"))

    @app.post("/film/{slug}/cap-and-rerun")
    def cap_and_rerun(slug: str, email: str = Depends(guard), budget: str = Form("")):
        """One click for a film the estimate stopped: the new cap into the row and, the film being blocked, a
        re-run, which stops at the cost gate with the estimate for the owner's approval. Nothing is approved here."""
        try:
            cap = float(budget)
            if not math.isfinite(cap) or cap <= 0:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "the budget cap must be a real number above zero") from None
        refused: dict = {}

        def change(row: dict):
            if not row["state"].startswith(("blocked:estimate", "blocked:no cap")):   # as cap_suggestion: only the
                refused["msg"] = (f"the film is {row['state'][:60]}, not stopped by the estimate: raise the cap under "
                                  "costs and use Retry; nothing changed")          # estimate's stops re-run to the gate
                return False
            row.update(budget_usd=money_text(cap), state="retry", updated=queue.now())
        edit_row(store, slug, change)
        if refused:
            return back(f"/film/{slug}", refused["msg"])
        return back(f"/film/{slug}", f"cap raised to {money_text(cap)} dollars; " + tick_now(
            "it stops at the cost gate with the estimate for your approval"))

    @app.post("/tick")
    def tick(request: Request, email: str = Depends(guard), next: str = Form("/")):
        if not (settings.project and settings.job):
            return back(next, "no runner is configured for this control room")
        try:
            busy = running_execution()
        except Exception:                                     # cannot tell: start one, as before this check existed
            busy = None
        if busy:
            return back(next, f"a tick is already running ({busy}); a second one would repeat its work")
        try:
            ex = runner_mod.run(settings.project, settings.region, settings.job)
        except Exception as exc:
            return back(next, f"could not start a tick: {type(exc).__name__}: {str(exc)[:120]}")
        return back(next, f"tick started ({ex}); it reads the queues and does one unit of work")

    @app.get("/references", response_class=HTMLResponse)
    def references(request: Request, email: str = Depends(guard)):
        from ..service import learn as learn_mod
        refs = load_rows(store, REFERENCES, queue.REFERENCE_COLUMNS)
        for r in refs:
            r.update(reference_price(r))
            pid = reference_id(r)
            if r["state"] == "done" and pid:              # what Gemini learned: its kinds, and the owner's correction
                profile = (films._json(store, f"custom_model_feed/notes/{pid}/profile.json")
                           or films._json(store, f"library/references/{pid}/profile.json") or {})   # (moved by a run)
                owner = films._json(store, f"custom_model_feed/notes/{pid}/{learn_mod.TAGS}")
                if owner is None:                                # a correction made before the move
                    owner = films._json(store, f"library/references/{pid}/{learn_mod.TAGS}")
                r.update(profile_id=pid, lessons=(profile.get("lessons") or [])[:3],
                         categories=[c for c in (owner if isinstance(owner, list) else profile.get("categories") or [])
                                     if c in learn_mod.CATEGORIES],
                         corrected=isinstance(owner, list))
        price = feed_price(feed_mod.DEFAULT_BUILD)
        pending = {p.rsplit("/", 1)[-1].removesuffix(".bgm.json") for p in store.list(feed_mod.REQUESTS)}
        for r in refs:
            if r.get("profile_id"):
                r.update(build_price=price, building=r["profile_id"] in pending)
        return page("references.html", request, email, refs=refs, categories=learn_mod.CATEGORIES,
                    build_count=feed_mod.DEFAULT_BUILD)

    def feed_price(count: int) -> float:
        """What building `count` music tracks costs, as the runner's ledger will hold it to."""
        from ..costing import lyria_price, load_pricing
        from ..providers.lyria import music_model
        from ..config import load_pipeline_config
        google = load_pipeline_config(None).get("google") or {}
        return feed_mod.build_price(count, lyria_price(load_pricing(None), music_model(google)))

    @app.post("/feed/build")
    def feed_build(email: str = Depends(guard), profile_id: str = Form(""), count: str = Form("10"),
                   usd: str = Form("")):
        """The owner approves building music tracks from a learned reference, at the price shown: saved as
        queue/feed/<id>.bgm.json, which the next tick takes once (service/feed.run_builds)."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile_id) or not (
                store.exists(f"custom_model_feed/notes/{profile_id}/profile.json")
                or store.exists(f"library/references/{profile_id}/profile.json")):
            return back("/references", "no such learned reference; nothing changed")
        try:
            n = int(count)
        except ValueError:
            n = 0
        if not 1 <= n <= feed_mod.MAX_TRACKS:
            return back("/references", f"Build between 1 and {feed_mod.MAX_TRACKS} tracks; nothing changed.")
        price = feed_price(n)                              # worked out here, never taken from the form
        try:
            shown = float(usd)
        except ValueError:
            shown = -1.0
        if abs(price - shown) > 0.01:
            return back("/references", f"the price is now ${price:.2f}; look again before approving. Nothing changed.")
        ask = f"{feed_mod.REQUESTS}{profile_id}.bgm.json"
        if store.exists(ask):
            return back("/feed", "that build is already waiting for the runner; nothing changed")
        store.write_text(ask, json.dumps({"by": email, "at": queue.now(), "count": n, "price": price,
                                          "approved_usd": round(price * 1.15, 2)}), "application/json")
        return back("/feed", f"{n} tracks approved at about ${price:.2f}; " + tick_now("the music is being made"))

    def sfx_state() -> tuple[list, float, bool]:
        """(the starter sounds the feed lacks, what making them costs, whether a build is asked for or under way)."""
        from ..costing import load_pricing
        have = {(films._json(store, rel) or {}).get("mood") for rel in store.list("custom_model_feed/sfx/")
                if rel.endswith(".json")}
        missing = feed_mod.missing_sfx(have)
        per = float((load_pricing(None).get("elevenlabs") or {}).get("sfx_per_sound", 0.03))
        busy = store.exists(f"{feed_mod.REQUESTS}starter.sfx.json") or \
            feed_mod.under_way(films._json(store, "custom_model_feed/builds/starter.sfx.json"))
        return missing, feed_mod.sfx_price(len(missing), per), busy

    @app.post("/feed/sfx-build")
    def sfx_build(email: str = Depends(guard), usd: str = Form("")):
        """The owner approves making the starter pack of sound effects (ElevenLabs) at the price shown: saved as
        queue/feed/starter.sfx.json for the next tick."""
        missing, price, busy = sfx_state()
        if busy:
            return back("/feed", "the sound effects are already being made; nothing changed")
        if not missing:
            return back("/feed", "every starter sound is in the library already; nothing changed")
        try:
            shown = float(usd)
        except ValueError:
            shown = -1.0
        if abs(price - shown) > 0.01:
            return back("/feed", f"the price is now ${price:.2f}; look again before approving. Nothing changed.")
        ask = f"{feed_mod.REQUESTS}starter.sfx.json"
        n = len(missing)
        store.write_text(ask, json.dumps({"by": email, "at": queue.now(), "count": n, "price": price,
                                          "approved_usd": round(price * 1.1, 2)}), "application/json")
        return back("/feed", f"{n} sound effects approved at about ${price:.2f}; " + tick_now("they are being made"))

    @app.get("/feed", response_class=HTMLResponse)
    def feed_page(request: Request, email: str = Depends(guard), mood: str = ""):
        tracks, sounds = [], []
        for kind, into in (("bgm", tracks), ("sfx", sounds)):
            for rel in store.list(f"custom_model_feed/{kind}/"):
                if not rel.endswith(".json"):
                    continue
                rec = films._json(store, rel)
                if not isinstance(rec, dict) or not rec.get("name") or (kind == "bgm" and mood and rec.get("mood") != mood):
                    continue
                into.append({**rec, "url": store.url(f"custom_model_feed/{kind}/{rec['name']}")})
        tracks.sort(key=lambda t: t.get("created") or "", reverse=True)
        sounds.sort(key=lambda t: t.get("name") or "")
        pending = [{**(films._json(store, rel) or {}), "reference": rel.rsplit("/", 1)[-1].split(".")[0],
                    "kind": "sfx" if rel.endswith(".sfx.json") else "bgm"}
                   for rel in store.list(feed_mod.REQUESTS) if rel.endswith((".bgm.json", ".sfx.json"))]
        builds = [b for rel in store.list("custom_model_feed/builds/") if rel.endswith(".json")
                  and isinstance(b := films._json(store, rel), dict)]
        sfx_missing, sfx_cost, sfx_busy = sfx_state()
        return page("feed.html", request, email, tracks=tracks, moods=feed_mod.MOODS, mood=mood, pending=pending,
                    builds=builds, refresh=30 if pending or sfx_busy else 0, sounds=sounds,
                    starter_count=len(sfx_missing), starter_price=sfx_cost, starter_waiting=sfx_busy,
                    starter_total=len(feed_mod.STARTER_SFX))

    def reference_id(r: dict) -> str:
        """The library folder of a learned reference, from where the runner wrote its profile."""
        parts = str(r.get("profile_uri") or "").replace("\\", "/").rstrip("/").split("/")
        pid = parts[-2] if len(parts) >= 2 and parts[-1] == "profile.json" else ""
        return pid if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", pid) else ""

    @app.post("/references/categories")
    async def reference_categories(request: Request, email: str = Depends(guard)):
        """The owner corrects what kinds of film a learned reference is an example of: written beside its profile
        (custom_model_feed/notes/<id>/tags.json), which wins over Gemini's and is never sent back by a run."""
        from ..service import learn as learn_mod
        form = await request.form()
        pid = str(form.get("profile_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", pid) or not (
                store.exists(f"custom_model_feed/notes/{pid}/profile.json")
                or store.exists(f"library/references/{pid}/profile.json")):
            return back("/references", "no such learned reference; nothing changed")
        chosen = [c for c in learn_mod.CATEGORIES if c in form.getlist("category")]
        store.write_text(f"custom_model_feed/notes/{pid}/{learn_mod.TAGS}", json.dumps(chosen), "application/json")
        return back("/references", f"categories saved: {', '.join(chosen) or 'none'}; the next film uses them")

    def reference_price(r: dict) -> dict:
        """What watching a reference costs, as the runner prices it: from the length on its row, else from the
        runner's own note on a watch it stopped (the length it read from YouTube), else as a 150 min film."""
        from ..costing import load_pricing
        from ..service import learn as learn_mod
        try:
            minutes = float(r.get("minutes") or 0) or learn_mod.DEFAULT_MINUTES
        except ValueError:
            minutes = learn_mod.DEFAULT_MINUTES
        out = {"estimate_usd": learn_mod.estimate_usd(minutes, pricing=load_pricing(None))[0],
               "known_length": bool(r.get("minutes"))}
        priced = re.search(r"this ([\d.]+) min video costs about \$([\d.]+)", r.get("note") or "")
        if r.get("state") == "blocked:LearnCostError" and priced:
            out.update(minutes=priced.group(1), estimate_usd=float(priced.group(2)), known_length=True)
        return out

    @app.post("/references/approve")
    def approve_reference(email: str = Depends(guard), url: str = Form(...), learn: str = Form(""),
                          usd: str = Form("")):
        """The owner approves a watch the cost guard stopped (over $1): the row gets the amount (with a tenth
        spare, as the token count is an estimate) and goes back to the runner."""
        try:
            shown = float(usd)
        except ValueError:
            shown = -1.0
        changed: list[str] = []
        refused: dict = {}

        def change(text: str) -> str | None:
            refs = queue.rows_of(text, queue.REFERENCE_COLUMNS)
            changed.clear()
            for r in refs:
                if r["url"] == url and (not learn or r["learn"] == learn) and r["state"] == "blocked:LearnCostError":
                    price = reference_price(r)["estimate_usd"]           # worked out here, never the form's
                    if abs(price - shown) > 0.01:
                        refused["msg"] = f"the price is now ${price:.2f}; look again before approving. Nothing changed."
                        return None
                    if not 0 < price <= queue.MAX_APPROVE_USD:
                        refused["msg"] = (f"${price:.2f} is over the ${queue.MAX_APPROVE_USD:.0f} one watch may be "
                                          "approved for here; nothing changed.")
                        return None
                    cap = min(price * 1.1, queue.MAX_APPROVE_USD)        # a tenth spare: the tokens are estimated
                    r.update(state="retry", approved_usd=f"{cap:.2f}", updated=queue.now(),
                             note=f"approved up to ${cap:.2f} by {email}")
                    changed.append(r["learn"])
            return queue.csv_of(refs, queue.REFERENCE_COLUMNS)
        store.update_text(REFERENCES, change, "text/csv")
        if refused:
            return back("/references", refused["msg"])
        if not changed:
            return back("/references", "nothing to approve: that watch is no longer waiting on its cost")
        return back("/references", f"approved about ${shown:.2f}; " + tick_now("Gemini is watching it"))

    @app.post("/references")
    def add_reference(email: str = Depends(guard), url: str = Form(...), title: str = Form(""),
                      learn: str = Form("all"), minutes: str = Form("")):
        url = url.strip()
        if not re.match(r"^https://(www\.)?(youtube\.com|youtu\.be)/", url):
            raise HTTPException(400, "a public YouTube link, please")
        if learn not in ("all", "score", "emotion", "action", "characters", "trailer"):
            raise HTTPException(400, "learn must be all, score, emotion, action, characters or trailer")
        if minutes:
            try:
                minutes = f"{float(minutes):g}"
            except ValueError:
                raise HTTPException(400, "minutes must be a number") from None
        else:                                             # read from the video's page, so the price is its own
            from ..service import learn as learn_mod
            found = learn_mod.video_minutes(url)
            minutes = f"{found:g}" if found else ""
        new = {"url": url, "title": title.strip(), "learn": learn, "minutes": minutes, "state": "new",
               "profile_uri": "", "note": "", "updated": queue.now(), "approved_usd": ""}

        def add(text: str) -> str:
            refs = queue.rows_of(text, queue.REFERENCE_COLUMNS)
            if any(r["url"] == url and r["learn"] == learn for r in refs):   # the same watch twice is paid twice
                raise HTTPException(400, f"that link is already queued to learn {learn}; set it to retry instead")
            return queue.csv_of(refs + [new], queue.REFERENCE_COLUMNS)
        store.update_text(REFERENCES, add, "text/csv")
        return back("/references", "queued; the next tick watches it (the cost guard applies)")

    @app.post("/references/retry")
    def retry_reference(email: str = Depends(guard), url: str = Form(...), learn: str = Form("")):
        def change(text: str) -> str:
            # only the clicked row (its link and focus), and only while it is blocked: the same link learned for
            # another focus may be done (re-learning it is paid again) or being learned right now (learning)
            refs = queue.rows_of(text, queue.REFERENCE_COLUMNS)
            changed.clear()
            for r in refs:
                if r["url"] == url and (not learn or r["learn"] == learn) and r["state"].startswith("blocked"):
                    r.update(state="retry", updated=queue.now())
                    changed.append(r["learn"])
            return queue.csv_of(refs, queue.REFERENCE_COLUMNS)
        changed: list[str] = []
        store.update_text(REFERENCES, change, "text/csv")
        return back("/references", "set to retry" if changed else "nothing to retry: that row is no longer blocked")

    if not store.is_gs:
        @app.get("/file/{path:path}")
        def file(path: str, email: str = Depends(guard)):
            p = store._local(path)
            if ".." in path or not p.is_file():
                raise HTTPException(404)
            return FileResponse(str(p))

    @app.exception_handler(HTTPException)
    async def on_error(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if exc.status_code == 403:                        # the denial page stands on its own, template or not
            return HTMLResponse(str(exc.detail), status_code=403)
        try:
            html = templates.get_template("message.html").render(
                {"request": request, "settings": settings, "status": exc.status_code, "detail": exc.detail,
                 "email": None, "root": store.root, "msg": None})
        except Exception:
            html = (f"<!doctype html><meta charset='utf-8'><h1>{int(exc.status_code)}</h1>"
                    f"<p>{_html.escape(str(exc.detail))}</p>")
        return HTMLResponse(html, status_code=exc.status_code)

    return app
