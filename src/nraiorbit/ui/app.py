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


def user_email(request: Request) -> str | None:
    raw = request.headers.get("x-goog-authenticated-user-email", "")
    return raw.split(":", 1)[-1].strip().lower() or None if raw else None


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
    settings = settings or Settings(auth=False)
    app = FastAPI(title=settings.title, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = lambda v: "" if v is None else f"${float(v):,.2f}"
    templates.env.filters["short"] = lambda v, n=90: (v if len(v) <= n else v[:n - 1] + "…") if isinstance(v, str) else v

    def guard(request: Request) -> str:
        if not settings.auth:
            return "local"
        email = user_email(request)
        if not email or email not in settings.allowed_emails:
            raise HTTPException(status_code=403, detail="This page is private.")
        return email

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

    # ------------------------------------------------------------------ pages
    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

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
        html = templates.get_template("message.html").render(
            {"request": request, "settings": settings, "status": exc.status_code, "detail": exc.detail,
             "email": None, "root": store.root, "msg": None})
        return HTMLResponse(html, status_code=exc.status_code)

    return app
