"""REST API (JSON, /api/*) for scripts and external tools.

Auth: when OMNIFETCH_TOKEN is set, every endpoint except /api/health
requires the token as X-OmniFetch-Token header, Authorization: Bearer,
?token= query param, or the omnifetch_token cookie (set by the Web UI).
When the token is empty, everything is open (LAN/trusted use).
"""
from __future__ import annotations

import hmac
import logging
import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRouter
from pydantic import BaseModel

from ..config import Settings
from ..core import db, statemachine, taskgen
from ..core.matcher import create_want as _create_want
from ..core.models import TaskStatus, WantStatus
from ..core.util import utcnow

log = logging.getLogger(__name__)


class WantIn(BaseModel):
    """Module level on purpose: with lazy annotations (PEP 649, Python 3.14+)
    FastAPI resolves parameter types via module globals — a model defined
    inside the factory function would not resolve and be misread as a
    query param."""

    work_key: str
    display_title: str = ""
    match_rule: dict[str, Any] = {}
    rank_rule: dict[str, Any] = {}


def check_token(request: Request, settings: Settings) -> None:
    """Raises 401 unless the token is unset or a provided one matches."""
    if not settings.token:
        return
    provided = (
        request.headers.get("x-omnifetch-token")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or request.query_params.get("token")
        or request.cookies.get("omnifetch_token")
        or ""
    )
    if not hmac.compare_digest(provided, settings.token):
        raise HTTPException(401, "invalid or missing token")


def get_candidates(conn: sqlite3.Connection, want_id: int) -> dict[str, Any]:
    """Candidate chain of one want, ranked by its rank_rule, each with its
    task state — shared by the JSON API and the Web UI fragment.

    One joined query only: resource rows carry everything resource_from_row
    needs, so there is no per-row refetch (the old N+1)."""
    want = db.fetch_want(conn, want_id)
    if want is None:
        raise HTTPException(404, "want not found")
    rows = conn.execute(
        """
        SELECT r.*,
               t.id AS task_id, t.downloader, t.status AS task_status,
               t.progress, t.retry_count, t.error_kind, t.error_message,
               t.external_id, t.updated_at AS task_updated_at
        FROM resources r LEFT JOIN tasks t ON t.resource_id = r.id
        WHERE r.want_id = ? ORDER BY r.id, t.id
        """,
        (want_id,),
    ).fetchall()
    candidates = []
    for row in rows:
        resource = db.resource_from_row(row)
        item = dict(row)
        for extra_key in ("id", "status", "quality", "updated_at", "source_url",
                          "download_uris", "normalized_title", "extra",
                          "want_id", "created_at", "task_updated_at"):
            item.pop(extra_key, None)
        item["resource_id"] = row["id"]
        item["resource_status"] = row["status"]
        item["updated_at"] = row["task_updated_at"]
        item["quality"] = resource.quality.__dict__
        item["score"] = taskgen.score(resource, want.rank_rule)
        item["uris"] = resource.download_uris
        candidates.append(item)
    # stable sort keeps id order within equal scores
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return {"want": {"id": want.id, "work_key": want.work_key,
                     "display_title": want.display_title, "status": want.status.value,
                     "rank_rule": want.rank_rule},
            "candidates": candidates}


def create_api_router(settings: Settings, ctx) -> APIRouter:
    router = APIRouter(prefix="/api")

    def get_conn() -> sqlite3.Connection:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def auth(request: Request) -> None:
        check_token(request, settings)

    @router.get("/health")
    def health() -> dict:
        return {"ok": True, "auth_required": bool(settings.token)}

    @router.get("/auth/verify", dependencies=[Depends(auth)])
    def auth_verify() -> dict:
        """Authenticated probe used by the token modal to validate input."""
        return {"ok": True}

    # ------------------------------------------------------------ reads
    @router.get("/stats", dependencies=[Depends(auth)])
    def stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        wants = {s.value: 0 for s in WantStatus}
        wants.update({r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM wants GROUP BY status")})
        tasks = {s.value: 0 for s in TaskStatus}
        tasks.update({r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status")})
        completed_24h = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='COMPLETED' AND updated_at >= ?",
            (utcnow().isoformat()[:10],),
        ).fetchone()[0]
        return {"wants": wants, "tasks": tasks, "completed_today": completed_24h}

    @router.get("/wants", dependencies=[Depends(auth)])
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

    @router.post("/wants", status_code=201, dependencies=[Depends(auth)])
    def create_want(body: WantIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            want, matched = _create_want(conn, body.work_key, body.display_title,
                                         body.match_rule, body.rank_rule)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"id": want.id, "work_key": want.work_key, "status": want.status.value,
                "matched_existing": matched}

    @router.get("/wants/{want_id}/candidates", dependencies=[Depends(auth)])
    def want_candidates(want_id: int,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        return get_candidates(conn, want_id)

    @router.get("/resources", dependencies=[Depends(auth)])
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

    @router.get("/tasks", dependencies=[Depends(auth)])
    def list_tasks(status: str | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
        if status:
            rows = conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT 500",
                                (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 500").fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------- operations
    @router.post("/tasks/{task_id}/retry", dependencies=[Depends(auth)])
    def retry_task(task_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not statemachine.retry_task(conn, task_id, utcnow()):
            raise HTTPException(404, "task not found or not in a retryable state")
        return {"ok": True}

    @router.post("/tasks/{task_id}/ignore", dependencies=[Depends(auth)])
    def ignore_task(task_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not statemachine.ignore_task(conn, task_id, utcnow()):
            raise HTTPException(404, "task not found or not in an ignorable state")
        return {"ok": True}

    @router.post("/wants/{want_id}/retry-next", dependencies=[Depends(auth)])
    def retry_next(want_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute("SELECT status FROM wants WHERE id=?", (want_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "want not found")
        if row["status"] == "ACQUIRED":
            raise HTTPException(409, "want already acquired; re-open it first")
        now = utcnow()
        # In-flight downloads must also be stopped on the tool side, or the
        # abandoned attempt keeps downloading in the background.
        for row in conn.execute(
            """
            SELECT downloader, external_id FROM tasks
            WHERE status='DOWNLOADING'
              AND resource_id IN (SELECT id FROM resources WHERE want_id=?)
            """,
            (want_id,),
        ).fetchall():
            downloader = ctx.downloaders.get(row["downloader"])
            if downloader is not None and row["external_id"]:
                try:
                    downloader.cancel(row["external_id"])
                except Exception:
                    log.warning("retry-next: cancel failed for %s",
                                row["external_id"], exc_info=True)
        conn.execute(
            """
            UPDATE tasks SET status='IGNORED', updated_at=?
            WHERE status IN ('QUEUED', 'DOWNLOADING', 'FAILED', 'FAILED_RETRYABLE')
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

    @router.post("/wants/{want_id}/reopen", dependencies=[Depends(auth)])
    def reopen_want(want_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if db.fetch_want(conn, want_id) is None:
            raise HTTPException(404, "want not found")
        statemachine.revive_want(conn, want_id, utcnow())
        return {"ok": True}

    return router
