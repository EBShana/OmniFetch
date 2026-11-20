"""Download dispatch + status polling.

dispatch(): claim QUEUED tasks (atomic, one at a time) and submit them to
their downloader. A missing/broken downloader fails the task as permanent so
the fallback chain keeps moving.

poll_active(): ask every DOWNLOADING task's downloader for its state and feed
the state machine: DONE -> complete, FAILED -> fail (retry or fallback),
PROGRESS -> update progress. Exceptions during poll are logged and skipped —
one bad poll must not burn a retry budget.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

from ..core import statemachine, taskgen
from ..core.db import fetch_task
from ..core.models import ErrorKind
from ..core.util import iso

if TYPE_CHECKING:
    from .runner import Context

log = logging.getLogger(__name__)


def dispatch(conn: sqlite3.Connection, ctx: "Context", now: datetime) -> int:
    dispatched = 0
    for _ in range(ctx.settings.max_dispatch_per_tick):
        task = statemachine.claim_task(conn, now)
        if task is None:
            break

        downloader = ctx.downloaders.get(task.downloader)
        if downloader is None:
            taskgen.on_task_failed(
                conn, task.id, ErrorKind.PERMANENT,
                f"downloader '{task.downloader}' not enabled",
                ctx.settings.retry, ctx.downloader_for, now,
            )
            continue

        from ..core.db import fetch_resource

        resource = fetch_resource(conn, task.resource_id)
        try:
            external_id = downloader.submit(resource)
        except Exception as exc:
            kind = getattr(exc, "kind", ErrorKind.TRANSIENT)
            taskgen.on_task_failed(
                conn, task.id, kind, str(exc),
                ctx.settings.retry, ctx.downloader_for, now,
            )
            log.warning("submit failed for task %s: %s", task.id, exc)
            continue

        statemachine.begin_download(conn, task.id, external_id, now)
        dispatched += 1
    return dispatched


def poll_active(conn: sqlite3.Connection, ctx: "Context", now: datetime) -> None:
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status='DOWNLOADING'"
    ).fetchall()
    for row in rows:
        task = fetch_task(conn, row["id"])
        if task is None or not task.external_id:
            continue
        downloader = ctx.downloaders.get(task.downloader)
        if downloader is None:
            continue
        try:
            result = downloader.poll(task.external_id)
        except Exception as exc:
            log.warning("poll failed for task %s: %s", task.id, exc)
            continue

        if result.status.value == "DONE":
            outcome = statemachine.complete_task(conn, task.id, now)
            if outcome == "WANT_ACQUIRED":
                ctx.notify("want.acquired", {"task_id": task.id, "resource_id": task.resource_id})
        elif result.status.value == "FAILED":
            outcome = taskgen.on_task_failed(
                conn, task.id,
                result.error_kind or ErrorKind.PERMANENT,
                result.error_message or "download failed",
                ctx.settings.retry, ctx.downloader_for, now,
            )
            if outcome in ("GIVE_UP", "NO_DOWNLOADER"):
                ctx.notify("task.failed", {"task_id": task.id})
            elif outcome == "NEW_TASK":
                ctx.notify("want.fallback", {"failed_task_id": task.id})
        else:
            statemachine.update_progress(conn, task.id, result.progress, now)
