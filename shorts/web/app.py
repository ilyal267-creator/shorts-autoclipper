"""The tester dashboard: server-rendered pages over the pipeline in `shorts/`."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db

HERE = Path(__file__).resolve().parent


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

    def page(request: Request, name: str, **context):
        context.setdefault("user", None)
        return templates.TemplateResponse(request, name, context)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def home():
        return RedirectResponse("/runs", status_code=303)

    @app.get("/runs")
    def runs(request: Request):
        return page(request, "runs.html", nav="runs", runs=[])

    return app
