"""The control room's routes. Reads through `films`, writes only the queue sheets and the stories folder."""

from __future__ import annotations

import csv
import datetime
import io
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
    session_secret: str | None = None        # signs the session cookie; random per deployment when unset
    session_days: int = 7


SESSION_COOKIE = "nraiorbit_session"


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
    text = store.read_text(rel) or ""
    rows = list(csv.DictReader(io.StringIO(text))) if text.strip() else []
    return [{c: (r.get(c) or "").strip() for c in columns} for r in rows]


def save_rows(store: Store, rel: str, rows: list[dict], columns: list[str]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    store.write_text(rel, buf.getvalue(), "text/csv")


def create_app(store: Store, settings: Settings | None = None) -> FastAPI:
    import secrets as _secrets
    settings = settings or Settings(auth=False)
    if not settings.session_secret:
        settings.session_secret = _secrets.token_hex(32)
    app = FastAPI(title=settings.title, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = lambda v: "" if v is None else f"${float(v):,.2f}"
    templates.env.filters["short"] = lambda v, n=90: (v if len(v) <= n else v[:n - 1] + "…") if isinstance(v, str) else v

    def guard(request: Request) -> str:
        """Who may act: the session cookie set by "Sign in with Google", or Identity-Aware Proxy's assertion when the
        service sits behind IAP. Anyone else is sent to the sign-in page (or told no, on a form post)."""
        if not settings.auth:
            return "local"
        email = read_session(settings.session_secret, request.cookies.get(SESSION_COOKIE)) or user_email(request)
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

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request):
        if not settings.auth:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "settings": settings, "email": None, "root": store.root,
            "msg": request.query_params.get("msg"), "client_id": settings.google_client_id,
            "origin": str(request.base_url).rstrip("/")})

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
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, sign_session(settings.session_secret, email, settings.session_days),
                        max_age=settings.session_days * 86400, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", path="/")
        print(f"control room: {email} signed in", flush=True)
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login?msg=signed+out", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, email: str = Depends(guard)):
        rows = load_rows(store, STORIES, queue.STORY_COLUMNS)
        by_slug = {r["slug"]: r for r in rows}
        cards = [films.summary(store, slug, by_slug.get(slug)) for slug in films.film_slugs(store, rows)]
        refs = load_rows(store, REFERENCES, queue.REFERENCE_COLUMNS)
        return page("home.html", request, email, films=cards, refs=refs, runner=runner_panel())

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
        rows.append({"slug": slug, "story_uri": store.uri(f"stories/{slug}.md"), "budget_usd": f"{budget:g}",
                     "gates": gates, "publish": publish, "state": "new", "note": "", "updated": queue.now()})
        save_rows(store, STORIES, rows, queue.STORY_COLUMNS)
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
        return page("film.html", request, email, f=d, stages=films.STAGES, runner=panel, log_lines=log_lines,
                    latest=latest)

    # ------------------------------------------------------------------ actions (the queue sheet only)
    def _row(slug: str):
        rows = load_rows(store, STORIES, queue.STORY_COLUMNS)
        row = next((r for r in rows if r["slug"] == slug), None)
        if row is None:
            raise HTTPException(400, f"{slug} is not in the queue; films made by hand are driven from Claude Code")
        return rows, row

    @app.post("/film/{slug}/approve")
    def approve(slug: str, email: str = Depends(guard), gate: str = Form("")):
        rows, row = _row(slug)
        row.update(state="approved", note=f"{gate} approved in the control room by {email}", updated=queue.now())
        save_rows(store, STORIES, rows, queue.STORY_COLUMNS)
        return back(f"/film/{slug}", f"{gate or 'gate'} approved; the next tick carries on")

    @app.post("/film/{slug}/retry")
    def retry(slug: str, email: str = Depends(guard)):
        rows, row = _row(slug)
        row.update(state="retry", updated=queue.now())
        save_rows(store, STORIES, rows, queue.STORY_COLUMNS)
        return back(f"/film/{slug}", "set to retry; the next tick runs it again")

    @app.post("/film/{slug}/row")
    def save_row(slug: str, email: str = Depends(guard), budget: str = Form(""), gates: str = Form(""),
                 publish: str = Form("")):
        rows, row = _row(slug)
        if budget:
            try:
                if float(budget) <= 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "the budget cap must be a number above zero") from None
            row["budget_usd"] = f"{float(budget):g}"
        if gates in ("ask", "auto"):
            row["gates"] = gates
        if publish in ("none", "private", "unlisted", "public"):
            row["publish"] = publish
        row["updated"] = queue.now()
        save_rows(store, STORIES, rows, queue.STORY_COLUMNS)
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
        refs = load_rows(store, REFERENCES, queue.REFERENCE_COLUMNS)
        refs.append({"url": url, "title": title.strip(), "learn": learn, "minutes": minutes, "state": "new",
                     "profile_uri": "", "note": "", "updated": queue.now()})
        save_rows(store, REFERENCES, refs, queue.REFERENCE_COLUMNS)
        return back("/references", "queued; the next tick watches it (the cost guard applies)")

    @app.post("/references/retry")
    def retry_reference(email: str = Depends(guard), url: str = Form(...)):
        refs = load_rows(store, REFERENCES, queue.REFERENCE_COLUMNS)
        for r in refs:
            if r["url"] == url:
                r.update(state="retry", updated=queue.now())
        save_rows(store, REFERENCES, refs, queue.REFERENCE_COLUMNS)
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
            html = f"<!doctype html><meta charset='utf-8'><h1>{exc.status_code}</h1><p>{exc.detail}</p>"
        return HTMLResponse(html, status_code=exc.status_code)

    return app
