"""Web UI: page shells + HTMX fragments + form actions.

Pages are static shells (no data) so they always render; fragments and
actions enforce the token — the shell pops a token prompt on the first
401 from any fragment.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from importlib.resources import files

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
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
from ..core.util import parse_iso, utcnow
from ..crawlers import crawler_types
from ..crawlers import default_config as crawler_default_config
from ..downloaders import default_config as downloader_default_config
from ..downloaders import downloader_types
from ..webapi.app import check_token, get_candidates

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
    "webhook_url": "Webhook 通知地址（留空禁用）",
    "token": "访问令牌（留空不启用认证）",
    "retry_max_retries": "最大重试次数",
    "retry_base_seconds": "退避基数（秒）",
    "retry_max_seconds": "退避上限（秒）",
}


_CORE_INT_KEYS = {
    "poll_interval", "reaper_stale_minutes", "stalled_after_minutes",
    "max_dispatch_per_tick", "max_active_downloads",
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

    def get_conn():
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def auth(request: Request) -> None:
        check_token(request, settings)

    def render(name: str, **kw) -> str:
        return templates.get_template(name).render(**kw)

    # ---------------------------------------------------------- pages
    def page(request: Request, name: str, page_id: str, **kw):
        return templates.TemplateResponse(request, name, {"page": page_id, **kw})

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
    def frag_dashboard(conn: sqlite3.Connection = Depends(get_conn)):
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
    def frag_wants(conn: sqlite3.Connection = Depends(get_conn)):
        return HTMLResponse(_wants_rows(conn))

    @router.get("/frag/wants/{want_id}/candidates", dependencies=[Depends(auth)])
    def frag_candidates(want_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            data = get_candidates(conn, want_id)
        except HTTPException:
            return HTMLResponse("<p class='muted'>作品不存在</p>")
        return HTMLResponse(render("partials/candidates.html", **data))

    @router.get("/frag/tasks", dependencies=[Depends(auth)])
    def frag_tasks(status: str = "", conn: sqlite3.Connection = Depends(get_conn)):
        return HTMLResponse(_tasks_rows(conn, status or None))

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
    def frag_failures(conn: sqlite3.Connection = Depends(get_conn)):
        return HTMLResponse(_failures_html(conn))

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
                       prefer_sources: str = Form(""),
                       conn: sqlite3.Connection = Depends(get_conn)):
        alias_list = [a.strip() for a in aliases.replace("，", ",").split(",") if a.strip()]
        src_list = [s.strip() for s in prefer_sources.split(",") if s.strip()]
        _, matched = _create_want(
            conn, work_key.strip(), display_title.strip(),
            {"aliases": alias_list} if alias_list else {},
            {"prefer_sources": src_list} if src_list else {},
        )
        message = f"作品 {work_key} 已创建"
        if matched:
            message += f"，匹配到 {matched} 条存量候选"
        return _toast(_wants_rows(conn), message)

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
                      status: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
        if statemachine.retry_task(conn, task_id, utcnow()):
            return _after_task_action(conn, f"任务 #{task_id} 已重新排队", view, want_id, status)
        raise HTTPException(404, "任务不存在或状态不允许")

    @router.post("/ui/tasks/{task_id}/ignore", dependencies=[Depends(auth)])
    def ui_ignore_task(task_id: int, view: str = Form(""), want_id: int = Form(0),
                       status: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
        if statemachine.ignore_task(conn, task_id, utcnow()):
            return _after_task_action(conn, f"任务 #{task_id} 已忽略", view, want_id, status)
        raise HTTPException(404, "任务不存在或状态不允许")

    @router.post("/ui/wants/{want_id}/retry-next", dependencies=[Depends(auth)])
    def ui_retry_next(want_id: int, view: str = Form(""),
                      conn: sqlite3.Connection = Depends(get_conn)):
        row = conn.execute("SELECT status FROM wants WHERE id=?", (want_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "want not found")
        if row["status"] == "ACQUIRED":
            raise HTTPException(409, "作品已获得，请先重新打开")
        now = utcnow()
        conn.execute(
            """
            UPDATE tasks SET status='IGNORED', updated_at=?
            WHERE status IN ('FAILED','FAILED_RETRYABLE','QUEUED')
              AND resource_id IN (SELECT id FROM resources WHERE want_id=?)
            """,
            (now.isoformat(), want_id),
        )
        conn.execute("UPDATE wants SET status='WANTED', updated_at=? WHERE id=?",
                     (now.isoformat(), want_id))
        conn.commit()
        outcome = taskgen.advance_want(conn, want_id, ctx.downloader_for, now)
        messages = {"NEW_TASK": "已切换到下一个候选", "GIVE_UP": "没有可用候选了",
                    "NO_DOWNLOADER": "没有可用下载器"}
        html = _failures_html(conn) if view == "failures" else _wants_rows(conn)
        return _toast(html, messages.get(outcome, outcome))

    @router.post("/ui/wants/{want_id}/reopen", dependencies=[Depends(auth)])
    def ui_reopen(want_id: int, view: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn)):
        if db.fetch_want(conn, want_id) is None:
            raise HTTPException(404, "want not found")
        statemachine.revive_want(conn, want_id, utcnow())
        html = _failures_html(conn) if view == "failures" else _wants_rows(conn)
        return _toast(html, "作品已重新打开")

    # ---------------------------------------------------------- settings
    def _settings_html() -> str:
        sources_cfg, dls_cfg = load_plugin_configs(settings.config_dir)

        def instances(cfgs: dict, kind: str) -> list[dict]:
            return [
                {"kind": kind, "name": name, "type": cfg.get("type", "?"),
                 "enabled": bool(cfg.get("enabled", True)),
                 "fields": {k: v for k, v in cfg.items()
                            if k not in ("name", "type", "enabled")}}
                for name, cfg in cfgs.items()
            ]

        return render(
            "partials/settings.html",
            core=core_settings_dict(settings),
            core_labels=_CORE_LABELS,
            core_readonly={"db_path": settings.db_path, "config_dir": settings.config_dir},
            sources=instances(sources_cfg, "source"),
            downloaders=instances(dls_cfg, "downloader"),
            crawler_types=crawler_types(),
            downloader_types=downloader_types(),
        )

    @router.get("/frag/settings", dependencies=[Depends(auth)])
    def frag_settings():
        return HTMLResponse(_settings_html())

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
        sources_cfg, dls_cfg = load_plugin_configs(settings.config_dir)
        current = (sources_cfg if kind == "source" else dls_cfg).get(name, {})
        data: dict = {"name": name, "type": ptype, "enabled": "enabled" in form}
        for key, value in form.items():
            if key == "enabled":
                continue
            data[key] = _coerce_like(str(value), current.get(key))
        try:
            save_plugin_config(settings.config_dir, kind, name, data)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            status = (ctx.reload_source(name) if kind == "source"
                      else ctx.reload_downloader(name))
        except Exception as exc:
            status = f"已保存，但加载失败：{exc}"
        return _toast(_settings_html(), f"{name}：{status}")

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
