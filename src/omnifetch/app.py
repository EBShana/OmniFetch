"""Application assembly: JSON API (/api/*) + Web UI (pages & fragments)
+ static assets, one FastAPI app, one container."""
from __future__ import annotations

from importlib.resources import files
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .core.util import utcnow
from .scheduler.runner import build_context
from .webapi.app import create_api_router
from .webui.auth import SESSION_COOKIE, session_secret, verify_session
from .webui.routes import create_ui_router

_UI_PAGES = {"/", "/wants", "/tasks", "/failures", "/status", "/settings"}


def _needs_ui_login(path: str) -> bool:
    return path in _UI_PAGES or path.startswith(("/frag/", "/ui/"))


def create_app(settings: Settings | None = None, ctx=None) -> FastAPI:
    if settings is None:
        settings = ctx.settings if ctx is not None else load_settings()
    ctx = ctx or build_context(settings)

    app = FastAPI(title="OmniFetch", version="0.1.2")
    app.state.ctx = ctx  # exposed for the settings UI live-reload and tests
    app.include_router(create_api_router(settings, ctx))
    app.include_router(create_ui_router(settings, ctx))
    app.mount("/static", StaticFiles(directory=str(files("omnifetch.webui") / "static")),
              name="static")

    @app.middleware("http")
    async def ui_login_guard(request: Request, call_next):
        """Gate pages/fragments behind the UI session; /api keeps token auth."""
        if not settings.ui_password_hash or not _needs_ui_login(request.url.path):
            return await call_next(request)
        secret = session_secret(settings.ui_password_hash)
        if verify_session(secret, request.cookies.get(SESSION_COOKIE), utcnow()):
            return await call_next(request)
        if request.headers.get("hx-request") == "true":
            # htmx follows this header; a bare 401 would only show an error.
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    return app
