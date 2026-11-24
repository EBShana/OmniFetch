"""Application assembly: JSON API (/api/*) + Web UI (pages & fragments)
+ static assets, one FastAPI app, one container."""
from __future__ import annotations

from importlib.resources import files

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .scheduler.runner import build_context
from .webapi.app import create_api_router
from .webui.routes import create_ui_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    ctx = build_context(settings)

    app = FastAPI(title="OmniFetch", version="0.1.0")
    app.state.ctx = ctx  # exposed for the settings UI live-reload and tests
    app.include_router(create_api_router(settings, ctx))
    app.include_router(create_ui_router(settings, ctx))
    app.mount("/static", StaticFiles(directory=str(files("omnifetch.webui") / "static")),
              name="static")
    return app
