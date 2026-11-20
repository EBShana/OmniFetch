"""REST API for the Web UI. Read endpoints plus the manual operations:
retry / ignore a task, force the next candidate of a want, create wants.

This layer is a thin wrapper over the core: all rules (state machine,
fallback chain) stay in core/, the API only triggers them.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from ..config import Settings, load_settings
from ..core import db, statemachine, taskgen
from ..core.matcher import find_or_create_want
from ..core.util import utcnow
from ..scheduler.runner import Context, build_context


class WantIn(BaseModel):
    """Module level on purpose: with lazy annotations (PEP 649, Python 3.14+)
    FastAPI resolves parameter types via module globals — a model defined
    inside create_app() would not resolve and be misread as a query param."""

    work_key: str
    display_title: str = ""
    match_rule: dict[str, Any] = {}
    rank_rule: dict[str, Any] = {}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    ctx: Context = build_context(settings)
    app = FastAPI(title="OmniFetch", version="0.1.0")

    def get_conn() -> sqlite3.Connection:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ reads
    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/wants")
    def list_wants(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT w.*,
                (SELECT COUNT(*) FROM resources r WHERE r.want_id = w.id) AS candidates,
                (SELECT COUNT(*) FROM resources r JOIN tasks t ON t.resource_id = r.id
                 WHERE r.want_id = w.id
                   AND t.status IN ('QUEUED','DOWNLOADING','FAILED_RETRYABLE')) AS active_tasks
            FROM wants w ORDER BY w.id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    @app.post("/wants", status_code=201)
    def create_want(body: WantIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        want = find_or_create_want(conn, body.work_key, body.display_title,
                                   body.match_rule, body.rank_rule)
        return {"id": want.id, "work_key": want.work_key, "status": want.status.value}

    @app.get("/resources")
    def list_resources(want_id: int | None = None, status: str | None = None,
                       conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
        clauses, params = [], []
        if want_id is not None:
            clauses.append("want_id=?")
            params.append(want_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(f"SELECT * FROM resources {where} ORDER BY id DESC LIMIT 500",
                            params).fetchall()
        return [dict(row) for row in rows]

    @app.get("/tasks")
    def list_tasks(status: str | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
        if status:
            rows = conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT 500",
                                (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 500").fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------- operations
    @app.post("/tasks/{task_id}/retry")
    def retry_task(task_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not statemachine.retry_task(conn, task_id, utcnow()):
            raise HTTPException(404, "task not found or not in a retryable state")
        return {"ok": True}

    @app.post("/tasks/{task_id}/ignore")
    def ignore_task(task_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not statemachine.ignore_task(conn, task_id, utcnow()):
            raise HTTPException(404, "task not found or not in an ignorable state")
        return {"ok": True}

    @app.post("/wants/{want_id}/retry-next")
    def retry_next(want_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute("SELECT status FROM wants WHERE id=?", (want_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "want not found")
        if row["status"] == "ACQUIRED":
            raise HTTPException(409, "want already acquired; re-open it first")
        now = utcnow()
        # Dismiss failed candidates of this want so the chain moves on.
        conn.execute(
            """
            UPDATE tasks SET status='IGNORED', updated_at=?
            WHERE status IN ('FAILED','FAILED_RETRYABLE','QUEUED')
              AND resource_id IN (SELECT id FROM resources WHERE want_id=?)
            """,
            (now.isoformat(), want_id),
        )
        conn.execute(
            "UPDATE wants SET status='WANTED', updated_at=? WHERE id=?",
            (now.isoformat(), want_id),
        )
        conn.commit()
        outcome = taskgen.advance_want(conn, want_id, ctx.downloader_for, now)
        return {"ok": True, "outcome": outcome}

    return app
