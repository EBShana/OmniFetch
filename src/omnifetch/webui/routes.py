"""Web UI: page shells + HTMX fragments + form actions.

Pages are static shells (no data) so they always render; fragments and
actions enforce the token — the shell pops a token prompt on the first
401 from any fragment.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import secrets
import sqlite3
import threading
import time
from datetime import timedelta
from importlib.resources import files
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import (
    Settings,
    _apply_core_dict,
    core_settings_dict,
    enabled,
    load_plugin_configs,
    remove_plugin_config,
    save_core_settings,
    save_plugin_config,
)
from ..core import db, statemachine, taskgen
from ..core.matcher import create_want as _create_want
from ..core.security import verify_password
from ..core.util import parse_iso, utcnow
from ..crawlers import crawler_types
from ..crawlers import default_config as crawler_default_config
from ..downloaders import default_config as downloader_default_config
from ..downloaders import dispatch_daily_limit, downloader_types
from ..downloaders.p115 import (P115Downloader, QrLoginSession,
                                parse_cookie_string, read_session_status,
                                write_cookie_file)
from ..webapi.app import check_token, get_candidates
from .auth import (SESSION_COOKIE, SESSION_TTL, LoginThrottle, create_session,
                   session_secret, verify_session)

templates = Jinja2Templates(directory=str(files("omnifetch.webui") / "templates"))

_BADGES = {
    "WANTED": "b-blue", "ACQUIRED": "b-green", "GIVEN_UP": "b-gray",
    "QUEUED": "b-blue", "DOWNLOADING": "b-blue", "FAILED_RETRYABLE": "b-orange",
    "FAILED": "b-red", "COMPLETED": "b-green", "IGNORED": "b-gray",
    "NEW": "b-gray", "TASKED": "b-blue", "REDUNDANT": "b-gray",
}
templates.env.globals["badge"] = lambda s: _BADGES.get(s, "badge")
templates.env.globals["pct"] = lambda x: f"{float(x or 0) * 100:.1f}%"


def _short_uri(uri: str) -> str:
    if uri.startswith("magnet:"):
        import re
        m = re.search(r"btih:([0-9a-zA-Z]{6})", uri)
        return f"magnet:{m.group(1)}…" if m else "magnet"
    return uri.rsplit("/", 1)[-1][:40] or uri[:40]


templates.env.globals["short_uri"] = _short_uri

_CORE_LABELS = {
    "poll_interval": "调度轮询间隔（秒）",
    "reaper_stale_minutes": "僵尸任务回收阈值（分钟）",
    "stalled_after_minutes": "停滞判定阈值（分钟）",
    "max_dispatch_per_tick": "每轮最多分发任务数",
    "max_active_downloads": "全局并发下载上限",
    "dispatch_daily_limit": "全局每日派发上限（0 = 不限）",
    "webhook_url": "Webhook 通知地址（留空禁用）",
    "token": "访问令牌（留空不启用认证）",
    "retry_max_retries": "最大重试次数",
    "retry_base_seconds": "退避基数（秒）",
    "retry_max_seconds": "退避上限（秒）",
}

_CORE_GROUPS = [
    ("调度", "保存即生效，调度循环下一轮读取", [
        "poll_interval", "reaper_stale_minutes", "stalled_after_minutes",
        "max_dispatch_per_tick", "max_active_downloads",
    ]),
    ("每日派发限额", "全局 0 = 不限；下载器卡片里可单独限额（p115 默认 50）", [
        "dispatch_daily_limit",
    ]),
    ("重试与退避", "失败重试的指数退避参数", [
        "retry_max_retries", "retry_base_seconds", "retry_max_seconds",
    ]),
    ("通知与认证", "留空即关闭对应功能", ["webhook_url", "token"]),
]

_FIELD_LABELS = {
    "cookie_file": "Cookie 文件路径",
    "target_dir_id": "目标目录 id（0 = 根目录）",
    "savepath": "保存子路径",
    "proxy": "抓种子代理（可留空）",
    "submit_grace_seconds": "提交宽限（秒）",
    "snapshot_ttl": "任务快照复用（秒）",
    "health_check_ttl": "健康检查缓存（秒）",
    "risk_cooldown_seconds": "风控冷却（秒）",
    "login_app": "扫码渠道（android/alipaymini/…）",
    "interval": "抓取间隔（秒）",
    "url": "地址",
    "resource_type": "资源类型",
    "max_pages_per_run": "每轮最多页数",
}

_P115_DEFAULT_COOKIE = P115Downloader.DEFAULT_CONFIG["cookie_file"]
templates.env.globals["field_label"] = lambda k: _FIELD_LABELS.get(k, k)

# Web UI 扫码登录的短期会话（进程内，单用户）。二维码 10 分钟有效。
_QR_SESSIONS: dict[str, dict] = {}
_QR_LOCK = threading.Lock()
_QR_TTL_SECONDS = 600
_P115_APPS = ("android", "alipaymini", "wechatmini", "tv", "ios",
              "qandroid", "web")


def _cookie_configured(cfg: dict) -> bool:
    """True when the instance's cookie jar exists and has entries.

    The file content is never rendered — only this boolean reaches the page.
    """
    path = Path(str(cfg.get("cookie_file") or _P115_DEFAULT_COOKIE))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data)


_CORE_INT_KEYS = {
    "poll_interval", "reaper_stale_minutes", "stalled_after_minutes",
    "max_dispatch_per_tick", "max_active_downloads", "dispatch_daily_limit",
    "retry_max_retries", "retry_base_seconds", "retry_max_seconds",
}


def _coerce_like(value: str, current):
    """Coerce a form string using the current value's type as the schema.

    Only fields already stored as numbers are converted; identifiers and
    secrets (e.g. want_key="001234") stay strings and keep leading zeros.
    """
    if isinstance(current, bool):
        return value == "true"
    if isinstance(current, int):
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(current, float):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _toast(html: str, message: str) -> Response:
    response = HTMLResponse(html)
    # ensure_ascii: header values must be latin-1; htmx JSON-parses the
    # trigger and restores unicode escapes client-side.
    response.headers["HX-Trigger"] = json.dumps({"toast": message})
    return response


def create_ui_router(settings: Settings, ctx) -> APIRouter:
    router = APIRouter()
    throttle = LoginThrottle()

    def auth(request: Request) -> None:
        check_token(request, settings)

    def render(name: str, **kw) -> str:
        return templates.get_template(name).render(**kw)

    def _safe_next(value: str) -> str:
        return value if value.startswith("/") and not value.startswith("//") else "/"

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _login_page(request: Request, *, error: str = "", username: str = "",
                    next_path: str = "/", locked: bool = False, status_code: int = 200):
        return templates.TemplateResponse(request, "login.html", {
            "error": error, "username": username or settings.ui_username,
            "next": _safe_next(next_path), "locked": locked,
        }, status_code=status_code)

    # ---------------------------------------------------------- login
    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = ""):
        if not settings.ui_password_hash:
            return RedirectResponse("/", status_code=303)
        secret = session_secret(settings.ui_password_hash)
        if verify_session(secret, request.cookies.get(SESSION_COOKIE), utcnow()):
            return RedirectResponse("/", status_code=303)
        return _login_page(request, next_path=next)

    @router.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, username: str = Form(""),
                     password: str = Form(""), next: str = Form("/")):
        if not settings.ui_password_hash:
            return RedirectResponse("/", status_code=303)
        key = _client_key(request)
        now = utcnow()
        if throttle.blocked(key, now):
            return _login_page(request, error="尝试次数过多，请 5 分钟后再试",
                               username=username, next_path=next, locked=True,
                               status_code=429)
        ok = (hmac.compare_digest(username, settings.ui_username)
              and verify_password(password, settings.ui_password_hash))
        if not ok:
            throttle.record_failure(key, now)
            time.sleep(throttle.delay)      # also slows scripted guessing
            return _login_page(request, error="用户名或密码不正确",
                               username=username, next_path=next, status_code=401)
        throttle.reset(key)
        response = RedirectResponse(_safe_next(next), status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            create_session(session_secret(settings.ui_password_hash),
                           settings.ui_username, now),
            max_age=int(SESSION_TTL.total_seconds()), httponly=True,
            samesite="lax", path="/", secure=request.url.scheme == "https",
        )
        return response

    @router.post("/logout")
    def logout():
        response = RedirectResponse("/login" if settings.ui_password_hash else "/",
                                    status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ---------------------------------------------------------- pages
    def page(request: Request, name: str, page_id: str, **kw):
        return templates.TemplateResponse(request, name, {
            "page": page_id, "ui_auth": bool(settings.ui_password_hash), **kw,
        })

    @router.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request):
        return page(request, "dashboard.html", "dashboard")

    @router.get("/wants", response_class=HTMLResponse)
    def page_wants(request: Request):
        return page(request, "wants.html", "wants")

    @router.get("/tasks", response_class=HTMLResponse)
    def page_tasks(request: Request, status: str = ""):
        return page(request, "tasks.html", "tasks", status=status)

    @router.get("/failures", response_class=HTMLResponse)
    def page_failures(request: Request):
        return page(request, "failures.html", "failures")

    @router.get("/status", response_class=HTMLResponse)
    def page_status(request: Request):
        return page(request, "status.html", "status")

    @router.get("/settings", response_class=HTMLResponse)
    def page_settings(request: Request):
        return page(request, "settings.html", "settings")

    # -------------------------------------------------------- queries
    def _wants_rows(conn: sqlite3.Connection) -> str:
        wants = [dict(r) for r in conn.execute(
            """
            SELECT w.*,
                (SELECT COUNT(*) FROM resources r WHERE r.want_id = w.id) AS candidates
            FROM wants w ORDER BY w.id
            """).fetchall()]
        for w in wants:
            w["active"] = conn.execute(
                """
                SELECT t.id, t.status, t.progress, t.downloader, r.source
                FROM tasks t JOIN resources r ON r.id = t.resource_id
                WHERE r.want_id = ? AND t.status IN ('QUEUED','DOWNLOADING','FAILED_RETRYABLE')
                ORDER BY t.id DESC LIMIT 1
                """,
                (w["id"],),
            ).fetchone()
        return render("partials/wants_rows.html", wants=wants)

    def _tasks_rows(conn: sqlite3.Connection, status: str | None) -> str:
        if status:
            rows = conn.execute(
                """
                SELECT t.*, r.title AS rtitle, r.source AS rsource
                FROM tasks t LEFT JOIN resources r ON r.id = t.resource_id
                WHERE t.status = ? ORDER BY t.id DESC LIMIT 200
                """,
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.*, r.title AS rtitle, r.source AS rsource
                FROM tasks t LEFT JOIN resources r ON r.id = t.resource_id
                ORDER BY t.id DESC LIMIT 200
                """
            ).fetchall()
        return render("partials/tasks_rows.html", tasks=[dict(r) for r in rows],
                      status=status or "")

    # ------------------------------------------------------- fragments
    @router.get("/frag/dashboard", dependencies=[Depends(auth)])
    def frag_dashboard():
        with db.session(settings.db_path) as conn:
            wants = {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM wants GROUP BY status")}
            tasks = {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status")}
            stats = {
                "wants": wants,
                "tasks": tasks,
                "completed_today": conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='COMPLETED' AND updated_at >= ?",
                    (utcnow().isoformat()[:10],),
                ).fetchone()[0],
            }
            runs = {}
            for r in conn.execute("SELECT * FROM source_runs ORDER BY id DESC LIMIT 200"):
                runs.setdefault(r["source"], dict(r))
            recent = [dict(r) for r in conn.execute(
                """
                SELECT t.id, t.updated_at, t.downloader, r.title, r.source
                FROM tasks t JOIN resources r ON r.id = t.resource_id
                WHERE t.status = 'COMPLETED' ORDER BY t.updated_at DESC LIMIT 8
                """)]
        return HTMLResponse(render("partials/dashboard.html",
                                   stats=stats, runs=runs, recent=recent))

    @router.get("/frag/wants", dependencies=[Depends(auth)])
    def frag_wants():
        with db.session(settings.db_path) as conn:
            html = _wants_rows(conn)
        return HTMLResponse(html)

    @router.get("/frag/wants/{want_id}/candidates", dependencies=[Depends(auth)])
    def frag_candidates(want_id: int):
        with db.session(settings.db_path) as conn:
            try:
                data = get_candidates(conn, want_id)
            except HTTPException:
                return HTMLResponse("<p class='muted'>作品不存在</p>")
        return HTMLResponse(render("partials/candidates.html", **data))

    @router.get("/frag/tasks", dependencies=[Depends(auth)])
    def frag_tasks(status: str = ""):
        with db.session(settings.db_path) as conn:
            html = _tasks_rows(conn, status or None)
        return HTMLResponse(html)

    def _failures_html(conn: sqlite3.Connection) -> str:
        failed = [dict(r) for r in conn.execute(
            """
            SELECT t.*, r.title, r.source FROM tasks t JOIN resources r ON r.id = t.resource_id
            WHERE t.status = 'FAILED' ORDER BY t.updated_at DESC LIMIT 100
            """)]
        given_up = [dict(r) for r in conn.execute(
            "SELECT * FROM wants WHERE status = 'GIVEN_UP' ORDER BY updated_at DESC LIMIT 100")]
        return render("partials/failures.html", failed=failed, given_up=given_up)

    @router.get("/frag/failures", dependencies=[Depends(auth)])
    def frag_failures():
        with db.session(settings.db_path) as conn:
            html = _failures_html(conn)
        return HTMLResponse(html)

    @router.get("/frag/status", dependencies=[Depends(auth)])
    def frag_status():
        sources_cfg, downloaders_cfg = load_plugin_configs(settings.config_dir)
        conn = db.connect(settings.db_path)
        try:
            latest: dict = {}
            for r in conn.execute("SELECT * FROM source_runs ORDER BY id DESC LIMIT 200"):
                latest.setdefault(r["source"], dict(r))
        finally:
            conn.close()
        sources = []
        for name, cfg in enabled(sources_cfg).items():
            run = latest.get(name)
            running = bool(run and not run.get("finished_at"))
            next_due = ""
            if run and not running:
                started = parse_iso(run["started_at"])
                if started:
                    next_due = (started + timedelta(seconds=int(cfg.get("interval", 900)))).isoformat()
            sources.append({"name": name, "type": cfg.get("type", "?"),
                            "interval": int(cfg.get("interval", 900)),
                            "run": run, "running": running, "next_due": next_due})
        downloaders = []
        for name, dl in ctx.downloaders.items():
            try:
                ok, detail = dl.ping()
            except Exception as exc:
                ok, detail = False, str(exc)
            downloaders.append({"name": name, "type": dl.type, "ok": ok, "detail": detail,
                                "accepts": ", ".join(a.value for a in dl.accepts)})
        return HTMLResponse(render("partials/status.html",
                                   sources=sources, downloaders=downloaders))

    # --------------------------------------------------------- actions
    @router.post("/ui/wants", dependencies=[Depends(auth)])
    def ui_create_want(work_key: str = Form(...),
                       display_title: str = Form(""),
                       aliases: str = Form(""),
                       prefer_sources: str = Form("")):
        alias_list = [a.strip() for a in aliases.replace("，", ",").split(",") if a.strip()]
        src_list = [s.strip() for s in prefer_sources.split(",") if s.strip()]
        with db.session(settings.db_path) as conn:
            _, matched = _create_want(
                conn, work_key.strip(), display_title.strip(),
                {"aliases": alias_list} if alias_list else {},
                {"prefer_sources": src_list} if src_list else {},
            )
            message = f"作品 {work_key} 已创建"
            if matched:
                message += f"，匹配到 {matched} 条存量候选"
            html = _wants_rows(conn)
        return _toast(html, message)

    def _after_task_action(conn, message: str, view: str, want_id: int,
                           status: str = "") -> Response:
        # Always re-render the fragment the click came from, otherwise the
        # failures/tasks page swaps in a different view (and loses its filter).
        if view == "candidates":
            try:
                data = get_candidates(conn, want_id)
                html = render("partials/candidates.html", **data)
            except HTTPException:
                html = "<p class='muted'>作品不存在</p>"
        elif view == "failures":
            html = _failures_html(conn)
        else:
            html = _tasks_rows(conn, status or None)
        return _toast(html, message)

    @router.post("/ui/tasks/{task_id}/retry", dependencies=[Depends(auth)])
    def ui_retry_task(task_id: int, view: str = Form(""), want_id: int = Form(0),
                      status: str = Form("")):
        with db.session(settings.db_path) as conn:
            if statemachine.retry_task(conn, task_id, utcnow()):
                return _after_task_action(conn, f"任务 #{task_id} 已重新排队",
                                          view, want_id, status)
        raise HTTPException(404, "任务不存在或状态不允许")

    @router.post("/ui/tasks/{task_id}/ignore", dependencies=[Depends(auth)])
    def ui_ignore_task(task_id: int, view: str = Form(""), want_id: int = Form(0),
                       status: str = Form("")):
        with db.session(settings.db_path) as conn:
            if statemachine.ignore_task(conn, task_id, utcnow()):
                return _after_task_action(conn, f"任务 #{task_id} 已忽略",
                                          view, want_id, status)
        raise HTTPException(404, "任务不存在或状态不允许")

    @router.post("/ui/wants/{want_id}/retry-next", dependencies=[Depends(auth)])
    def ui_retry_next(want_id: int, view: str = Form("")):
        with db.session(settings.db_path) as conn:
            try:
                outcome = taskgen.retry_next(conn, want_id, ctx.downloader_for,
                                             ctx.cancel_download, utcnow())
            except ValueError:
                raise HTTPException(409, "作品已获得，请先重新打开")
            if outcome is None:
                raise HTTPException(404, "want not found")
            messages = {"NEW_TASK": "已切换到下一个候选", "GIVE_UP": "没有可用候选了",
                        "NO_DOWNLOADER": "没有可用下载器"}
            html = _failures_html(conn) if view == "failures" else _wants_rows(conn)
        return _toast(html, messages.get(outcome, outcome))

    @router.post("/ui/wants/{want_id}/reopen", dependencies=[Depends(auth)])
    def ui_reopen(want_id: int, view: str = Form("")):
        with db.session(settings.db_path) as conn:
            if db.fetch_want(conn, want_id) is None:
                raise HTTPException(404, "want not found")
            statemachine.revive_want(conn, want_id, utcnow())
            html = _failures_html(conn) if view == "failures" else _wants_rows(conn)
        return _toast(html, "作品已重新打开")

    # ---------------------------------------------------------- settings
    def _settings_html() -> str:
        sources_cfg, dls_cfg = load_plugin_configs(settings.config_dir)

        def instances(cfgs: dict, kind: str) -> list[dict]:
            out = []
            for name, cfg in cfgs.items():
                ptype = cfg.get("type", "?")
                fields = {k: v for k, v in cfg.items()
                          if k not in ("name", "type", "enabled")}
                item = {"kind": kind, "name": name, "type": ptype,
                        "enabled": bool(cfg.get("enabled", True)), "fields": fields}
                if kind == "downloader" and ptype == "p115":
                    # 即使 toml 里缺字段也渲染出可编辑项，保存后即写入配置。
                    for key, default in P115Downloader.DEFAULT_CONFIG.items():
                        fields.setdefault(key, default)
                    # Rendered in dedicated sections rather than the generic grid.
                    fields.pop("dispatch_daily_limit", None)
                    item["daily_limit"] = dispatch_daily_limit(ptype, cfg)
                    item["cookie_configured"] = _cookie_configured(cfg)
                    item["cookie_file"] = str(cfg.get("cookie_file")
                                              or _P115_DEFAULT_COOKIE)
                    status = read_session_status(item["cookie_file"])
                    item["cookie_state"] = status
                    item["cookie_detail"] = str(status.get("detail") or "")
                    if status.get("checked_at"):
                        item["cookie_checked"] = time.strftime(
                            "%m-%d %H:%M",
                            time.localtime(float(status["checked_at"])))
                    api = getattr(ctx.downloaders.get(name), "_api", None)
                    if api is not None and getattr(api, "in_cooldown", None) \
                            and api.in_cooldown():
                        item["cooldown_remaining"] = api.cooldown_remaining()
                out.append(item)
            return out

        return render(
            "partials/settings.html",
            core=core_settings_dict(settings),
            core_labels=_CORE_LABELS,
            core_groups=_CORE_GROUPS,
            core_int_keys=_CORE_INT_KEYS,
            core_readonly={"db_path": settings.db_path, "config_dir": settings.config_dir},
            sources=instances(sources_cfg, "source"),
            downloaders=instances(dls_cfg, "downloader"),
            crawler_types=crawler_types(),
            downloader_types=downloader_types(),
            p115_apps=_P115_APPS,
        )

    @router.get("/frag/settings", dependencies=[Depends(auth)])
    def frag_settings():
        return HTMLResponse(_settings_html())

    def _drop_qr_session(sid: str) -> None:
        with _QR_LOCK:
            entry = _QR_SESSIONS.pop(sid, None)
        if entry is not None:
            try:
                entry["session"].close()
            except Exception:
                pass

    @router.post("/ui/p115/qr/start", dependencies=[Depends(auth)])
    def ui_p115_qr_start(name: str = Form(...)):
        _, dls_cfg = load_plugin_configs(settings.config_dir)
        cfg = dls_cfg.get(name) or {}
        if cfg.get("type") != "p115":
            return HTMLResponse('<span class="err-text">p115 实例不存在</span>')
        cookie_file = str(cfg.get("cookie_file") or _P115_DEFAULT_COOKIE)
        app = str(cfg.get("login_app") or "android")
        try:
            session = QrLoginSession(cookie_file, app=app, fallback_web=False)
        except Exception as exc:
            return HTMLResponse(
                '<span class="err-text">生成二维码失败：'
                f'{html.escape(str(exc))}</span>')
        sid = secrets.token_urlsafe(18)
        now = time.time()
        with _QR_LOCK:
            for old_sid, entry in list(_QR_SESSIONS.items()):
                if now - entry["created"] > _QR_TTL_SECONDS:
                    entry["session"].close()
                    _QR_SESSIONS.pop(old_sid, None)
            _QR_SESSIONS[sid] = {"session": session, "name": name,
                                 "app": app, "created": now,
                                 "lock": threading.Lock()}
        b64 = base64.b64encode(session.png_bytes()).decode("ascii")
        return HTMLResponse(
            '<div class="p115-qr">'
            f'<img alt="115 登录二维码" src="data:image/png;base64,{b64}">'
            '<div class="small muted">请用手机 115 App 扫码（'
            f'{html.escape(app)} 渠道），确认后自动保存 Cookie</div>'
            f'<div hx-get="/ui/p115/qr/{sid}/status" hx-trigger="every 2s" '
            'hx-target="this" hx-swap="innerHTML">等待扫码…</div>'
            '</div>')

    @router.get("/ui/p115/qr/{sid}/status", dependencies=[Depends(auth)])
    def ui_p115_qr_status(sid: str):
        entry = _QR_SESSIONS.get(sid)
        if entry is None:
            return HTMLResponse(
                '<span class="err-text">二维码会话已失效，请重新生成</span>')
        if time.time() - entry["created"] > _QR_TTL_SECONDS:
            _drop_qr_session(sid)
            return HTMLResponse(
                '<span class="err-text">二维码已过期，请重新生成</span>')
        with entry["lock"]:
            try:
                state, message = entry["session"].poll()
            except Exception as exc:
                state, message = "error", f"扫码登录异常：{exc}"
        if state == "waiting":
            return HTMLResponse(
                f'<span class="small muted">{html.escape(message)}</span>')
        _drop_qr_session(sid)
        if state == "done":
            try:
                ctx.reload_downloader(entry["name"])
            except Exception:
                pass
            return HTMLResponse(
                '<div class="small" style="color:#2e7d32">'
                f'{html.escape(message)}</div>'
                '<div hx-get="/frag/settings" hx-trigger="load" '
                'hx-target="closest .refresh-box" hx-swap="innerHTML"></div>')
        safe_name = html.escape(entry["name"])
        return HTMLResponse(
            f'<div class="small err-text">{html.escape(message)}</div>'
            '<button class="btn mini" type="button" '
            'hx-post="/ui/p115/qr/start" '
            f'hx-vals=\'{{"name": "{safe_name}"}}\' '
            f'hx-target="#p115-login-box-{safe_name}" '
            'hx-swap="innerHTML">重新生成</button>')

    @router.post("/ui/settings/core", dependencies=[Depends(auth)])
    async def ui_save_core(request: Request):
        form = await request.form()
        data = core_settings_dict(settings)  # untouched keys keep current values
        for key in data:
            if key in form:
                raw = str(form[key])
                if key in _CORE_INT_KEYS:
                    try:
                        data[key] = int(raw)
                    except ValueError:
                        raise HTTPException(400, f"{key} 必须是整数")
                else:
                    data[key] = raw
        # Validate everything before touching the file, so a bad form value
        # can never corrupt core.toml.
        save_core_settings(settings.config_dir, data)
        _apply_core_dict(settings, data)  # live-apply: settings IS ctx.settings
        ctx.rebuild_notifiers()  # webhook_url may have changed
        return _toast(_settings_html(), "核心设置已保存并生效")

    @router.post("/ui/settings/{kind}/{name}", dependencies=[Depends(auth)])
    async def ui_save_plugin(kind: str, name: str, request: Request):
        if kind not in ("source", "downloader"):
            raise HTTPException(404, "unknown kind")
        form = dict(await request.form())
        ptype = str(form.pop("type", ""))
        # Cookie input is transient: it is written to the cookie_file and never
        # stored in the TOML, echoed back, or logged.
        cookie_text = str(form.pop("cookie", ""))
        clear_cookie = "clear_cookie" in form
        form.pop("clear_cookie", None)
        sources_cfg, dls_cfg = load_plugin_configs(settings.config_dir)
        current = (sources_cfg if kind == "source" else dls_cfg).get(name, {})
        data: dict = {"name": name, "type": ptype, "enabled": "enabled" in form}
        for key, value in form.items():
            if key == "enabled":
                continue
            if key == "dispatch_daily_limit":
                try:
                    limit = int(str(value))
                except ValueError:
                    raise HTTPException(400, "每日派发上限必须是非负整数")
                if limit < 0:
                    raise HTTPException(400, "每日派发上限必须是非负整数")
                data[key] = limit
                continue
            data[key] = _coerce_like(str(value), current.get(key))
        cookie_note = ""
        if kind == "downloader" and ptype == "p115":
            cookie_path = str(data.get("cookie_file") or current.get("cookie_file")
                              or _P115_DEFAULT_COOKIE)
            data["cookie_file"] = cookie_path
            if clear_cookie:
                try:
                    Path(cookie_path).unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise HTTPException(400, f"无法删除 Cookie 文件：{exc}")
                cookie_note = "，Cookie 已清空"
            elif cookie_text.strip():
                try:
                    jar = parse_cookie_string(cookie_text)
                except ValueError as exc:
                    raise HTTPException(400, str(exc))
                try:
                    write_cookie_file(cookie_path, jar)
                except OSError as exc:
                    raise HTTPException(400, f"无法写入 Cookie 文件：{exc}")
                cookie_note = "，Cookie 已保存"
        try:
            save_plugin_config(settings.config_dir, kind, name, data)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            status = (ctx.reload_source(name) if kind == "source"
                      else ctx.reload_downloader(name))
        except Exception as exc:
            status = f"已保存，但加载失败：{exc}"
        return _toast(_settings_html(), f"{name}：{status}{cookie_note}")

    @router.post("/ui/settings/add", dependencies=[Depends(auth)])
    def ui_add_plugin(kind: str = Form(...), type: str = Form(...), name: str = Form(...)):
        registry = crawler_types() if kind == "source" else downloader_types()
        if type not in registry:
            raise HTTPException(400, "未知的插件类型")
        clean = name.strip()
        if not clean:
            raise HTTPException(400, "名称不能为空")
        defaults = (crawler_default_config if kind == "source"
                    else downloader_default_config)(type)
        try:
            save_plugin_config(settings.config_dir, kind, clean,
                               {**defaults, "name": clean, "type": type, "enabled": False})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return _toast(_settings_html(), f"已创建 {clean}（默认停用，补全配置后启用）")

    @router.post("/ui/settings/delete/{kind}/{name}", dependencies=[Depends(auth)])
    def ui_delete_plugin(kind: str, name: str):
        try:
            removed = remove_plugin_config(settings.config_dir, kind, name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        (ctx.crawlers if kind == "source" else ctx.downloaders).pop(name, None)
        message = f"已删除 {name}" if removed else f"配置文件不存在（已从运行中移除）：{name}"
        return _toast(_settings_html(), message)

    return router
