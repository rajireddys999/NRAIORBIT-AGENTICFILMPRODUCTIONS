"""The control room's routes. Reads through `films`, writes only the queue sheets and the stories folder."""

from __future__ import annotations

import datetime
import html as _html
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..service import queue
from . import films
from . import runner as runner_mod
from .store import Store

TEMPLATES_DIR = Path(__file__).parent / "templates"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
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

    def page(name: str, request: Request, email: str, **kw):
        ctx = {"request": request, "settings": settings, "email": email, "root": store.root, "is_gs": store.is_gs,
               "msg": request.query_params.get("msg"), "now": datetime.datetime.now(datetime.timezone.utc), **kw}
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, msg: str) -> RedirectResponse:
        from urllib.parse import quote
        return RedirectResponse(f"{url}?msg={quote(msg)}", status_code=303)

    def runner_panel() -> dict:
        panel = {"project": settings.project, "job": settings.job, "executions": [], "error": None}
        try:
            panel["executions"] = runner_mod.executions(settings.project, settings.region, settings.job)
        except Exception as exc:                       # no roles, no network, no job: the page still renders
            panel["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return panel

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
        return page("home.html", request, email, films=cards, refs=refs, runner=runner_panel(), youtube=youtube_status())

    @app.get("/new", response_class=HTMLResponse)
    def new_form(request: Request, email: str = Depends(guard)):
        return page("new.html", request, email)

    @app.post("/new")
    def new_story(request: Request, email: str = Depends(guard), slug: str = Form(...), story: str = Form(...),
                  budget: float = Form(...), runtime: int = Form(10), gates: str = Form("ask"),
                  publish: str = Form("private"), start: str = Form("")):
        slug = slug.strip().lower()
        if not SLUG_RE.match(slug):
            raise HTTPException(400, "slug: lowercase letters, digits and dashes, 2 to 41 characters")
        if len(story.strip()) < 40:
            raise HTTPException(400, "the story is too short to plan a film from")
        if budget <= 0:
            raise HTTPException(400, "the budget cap must be above zero: nothing is paid without one")
        if gates not in ("ask", "auto") or publish not in ("none", "private", "unlisted", "public"):
            raise HTTPException(400, "gates must be ask or auto; publish none, private, unlisted or public")
        rows = load_rows(store, STORIES, queue.STORY_COLUMNS)
        if any(r["slug"] == slug for r in rows) or store.exists(f"{slug}/project/pipeline.yaml"):
            raise HTTPException(400, f"{slug} exists already; pick another slug")
        store.write_text(f"stories/{slug}.md", story.strip() + "\n", "text/markdown")
        store.write_text(f"stories/{slug}.overrides.yaml",
                         yaml.safe_dump({"film": {"runtime_minutes": max(1, min(int(runtime), 60))}}), "text/yaml")

        def add(text: str) -> str:                        # under the precondition: a tick's claim lands beside it
            rows = queue.rows_of(text, queue.STORY_COLUMNS)
            if any(r["slug"] == slug for r in rows):
                raise HTTPException(400, f"{slug} exists already; pick another slug")
            rows.append({"slug": slug, "story_uri": store.uri(f"stories/{slug}.md"), "budget_usd": f"{budget:g}",
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
        d = films.detail(store, slug, row)
        log_lines, latest = [], None
        panel = runner_panel()
        if panel["executions"]:
            latest = panel["executions"][0]["name"]
            try:
                log_lines = runner_mod.logs(settings.project, latest)
            except Exception as exc:
                log_lines = [f"(log unavailable: {type(exc).__name__})"]
        running = bool(panel["executions"]) and panel["executions"][0]["state"] == "running"
        return page("film.html", request, email, f=d, stages=films.STAGES, runner=panel, log_lines=log_lines,
                    latest=latest, runner_running=running, youtube=youtube_status())

    # ------------------------------------------------------------------ actions (the queue sheet only)
    @app.post("/film/{slug}/approve")
    def approve(slug: str, email: str = Depends(guard), gate: str = Form("")):
        if gate not in queue.GATES:
            raise HTTPException(400, f"no gate called {gate[:40]!r}; one of {', '.join(queue.GATES)}")
        refused: dict = {}

        def change(row: dict):
            if row["state"] == "approved":                    # pressed twice: one approval, one tick
                refused["msg"] = "already approved; the runner takes it up on its next tick"
                return False
            if row["state"] != f"awaiting:{gate}":            # a stale page, or a run under way: never touch the claim
                refused["msg"] = f"nothing waits at the {gate} gate: the film is {row['state']}; nothing changed"
                return False
            row.update(state="approved", note=f"{gate} approved in the control room by {email}", updated=queue.now())
        edit_row(store, slug, change)
        if refused:
            return back(f"/film/{slug}", refused["msg"])
        msg = f"{gate} approved; the next tick carries on"
        if settings.project and settings.job:                 # no fifteen-minute wait: the next step starts now
            try:
                busy = [e for e in runner_mod.executions(settings.project, settings.region, settings.job)
                        if e["state"] == "running"]
                if busy:                                      # never two executions on the queue at once
                    msg = f"{gate} approved; a tick is running ({busy[0]['name']}), the next scheduled one carries on"
                else:
                    ex = runner_mod.run(settings.project, settings.region, settings.job)
                    msg = f"{gate} approved; a tick started ({ex}) and the next step is under way"
            except Exception as exc:
                msg = f"{gate} approved; the tick could not be started ({type(exc).__name__}), " \
                      f"the scheduled one carries on"
        return back(f"/film/{slug}", msg)

    @app.post("/film/{slug}/retry")
    def retry(slug: str, email: str = Depends(guard)):
        edit_row(store, slug, lambda row: row.update(state="retry", updated=queue.now()))
        return back(f"/film/{slug}", "set to retry; the next tick runs it again")

    @app.post("/film/{slug}/row")
    def save_row(slug: str, email: str = Depends(guard), budget: str = Form(""), gates: str = Form(""),
                 publish: str = Form("")):
        if budget:
            try:
                if float(budget) <= 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "the budget cap must be a number above zero") from None

        def change(row: dict):
            if budget:
                row["budget_usd"] = f"{float(budget):g}"
            if gates in ("ask", "auto"):
                row["gates"] = gates
            if publish in ("none", "private", "unlisted", "public"):
                row["publish"] = publish
            row["updated"] = queue.now()
        edit_row(store, slug, change)
        return back(f"/film/{slug}", "saved; the next tick writes it into the project")

    @app.post("/tick")
    def tick(request: Request, email: str = Depends(guard), next: str = Form("/")):
        if not (settings.project and settings.job):
            return back(next, "no runner is configured for this control room")
        try:
            ex = runner_mod.run(settings.project, settings.region, settings.job)
        except Exception as exc:
            return back(next, f"could not start a tick: {type(exc).__name__}: {str(exc)[:120]}")
        return back(next, f"tick started ({ex}); it reads the queues and does one unit of work")

    @app.get("/references", response_class=HTMLResponse)
    def references(request: Request, email: str = Depends(guard)):
        refs = load_rows(store, REFERENCES, queue.REFERENCE_COLUMNS)
        return page("references.html", request, email, refs=refs)

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
        new = {"url": url, "title": title.strip(), "learn": learn, "minutes": minutes, "state": "new",
               "profile_uri": "", "note": "", "updated": queue.now()}
        store.update_text(REFERENCES, lambda text: queue.csv_of(
            queue.rows_of(text, queue.REFERENCE_COLUMNS) + [new], queue.REFERENCE_COLUMNS), "text/csv")
        return back("/references", "queued; the next tick watches it (the cost guard applies)")

    @app.post("/references/retry")
    def retry_reference(email: str = Depends(guard), url: str = Form(...)):
        def change(text: str) -> str:
            refs = queue.rows_of(text, queue.REFERENCE_COLUMNS)
            for r in refs:
                if r["url"] == url:
                    r.update(state="retry", updated=queue.now())
            return queue.csv_of(refs, queue.REFERENCE_COLUMNS)
        store.update_text(REFERENCES, change, "text/csv")
        return back("/references", "set to retry")

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
