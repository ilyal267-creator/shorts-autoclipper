"""The tester dashboard: server-rendered pages over the pipeline in `shorts/`."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import auth as pipeline_auth
from . import auth, db, review, runs as runs_pages

HERE = Path(__file__).resolve().parent
PUBLIC_PATHS = ("/signin", "/auth/", "/health", "/static/")

SIGNIN_ERRORS = {
    "not_invited": "That Google account has no invite. Ask the person who invited you which email they used.",
    "failed": "Sign-in didn't complete. Try again.",
    "not_configured": "Google sign-in isn't set up on this server yet.",
}


def data_dir() -> Path:
    return Path(os.getenv("SHORTS_DATA_DIR", "data")).resolve()


def create_app(data: Path | None = None) -> FastAPI:
    data = (data or data_dir()).resolve()
    app = FastAPI(title="Shorts Autoclipper", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.data_dir = data
    app.state.db_path = data / "shorts.db"
    db.migrate(app.state.db_path)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def public_url(request: Request) -> str:
        return (os.getenv("SHORTS_PUBLIC_URL") or str(request.base_url)).rstrip("/")

    def secure_cookies(request: Request) -> bool:
        return public_url(request).startswith("https://")

    def page(request: Request, name: str, **context):
        context.setdefault("user", getattr(request.state, "user", None))
        return templates.TemplateResponse(request, name, context)

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        request.state.user = auth.user_for(app.state.db_path, request.cookies.get(auth.SESSION_COOKIE))
        if request.state.user is None and not request.url.path.startswith(PUBLIC_PATHS):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "sign in first"}, status_code=401)
            return RedirectResponse("/signin", status_code=303)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True}

    # ------------------------------------------------------------------ sign-in

    @app.get("/signin")
    def signin(request: Request, error: str = ""):
        if request.state.user:
            return RedirectResponse("/runs", status_code=303)
        return page(request, "signin.html", error=SIGNIN_ERRORS.get(error))

    @app.get("/auth/google")
    def google_start(request: Request):
        client = auth.google_client()
        if not client:
            return RedirectResponse("/signin?error=not_configured", status_code=303)
        state = secrets.token_urlsafe(24)
        verifier, challenge = pipeline_auth.pkce_pair()
        redirect_uri = public_url(request) + "/auth/google/callback"
        response = RedirectResponse(auth.consent_url(client, redirect_uri, state, challenge), status_code=303)
        response.set_cookie(
            auth.STATE_COOKIE, state + "." + verifier, max_age=600, httponly=True,
            samesite="lax", secure=secure_cookies(request), path="/auth/google",
        )
        return response

    @app.get("/auth/google/callback")
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        stored = request.cookies.get(auth.STATE_COOKIE, "")
        expected, _, verifier = stored.partition(".")
        client = auth.google_client()
        response = RedirectResponse("/signin?error=failed", status_code=303)
        response.delete_cookie(auth.STATE_COOKIE, path="/auth/google")
        if error or not code or not client or not expected or not secrets.compare_digest(expected, state):
            return response
        try:
            person = auth.exchange(client, code, verifier, public_url(request) + "/auth/google/callback")
        except auth.SignInError:
            return response
        token = auth.start_session(app.state.db_path, person["email"], person["name"])
        if token is None:
            response.headers["location"] = "/signin?error=not_invited"
            return response
        response.headers["location"] = "/runs"
        response.set_cookie(
            auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
            samesite="lax", secure=secure_cookies(request),
        )
        return response

    @app.post("/signout")
    def signout(request: Request):
        auth.end_session(app.state.db_path, request.cookies.get(auth.SESSION_COOKIE))
        response = RedirectResponse("/signin", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

    # ------------------------------------------------------------------ pages

    @app.get("/")
    def home():
        return RedirectResponse("/runs", status_code=303)

    @app.get("/runs")
    def runs(request: Request):
        listed = runs_pages.list_runs(app.state.db_path, request.state.user["id"])
        active = any(r["status"] in ("queued", "running") for r in listed)
        return page(request, "runs.html", nav="runs", runs=listed, active=active)

    runs_pages.register(app, page)
    review.register(app, page)

    return app
