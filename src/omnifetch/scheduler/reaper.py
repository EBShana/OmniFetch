"""Zombie task sweeper.

A task stuck in DOWNLOADING whose last heartbeat (updated_at, refreshed on
every successful poll) is older than the stale threshold is a zombie: the
scheduler process died (container restart/upgrade) while the download was
tracked. Requeue it and best-effort cancel the tool-side job — on the next
dispatch the same or another downloader will pick it up.

Idempotent by construction: only rows still in DOWNLOADING are touched.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..core.util import iso

if TYPE_CHECKING:
    from .runner import Context


def sweep(conn: sqlite3.Connection, ctx: "Context", now: datetime) -> int:
    cutoff = iso(now - ctx.settings.reaper_stale)
    rows = conn.execute(
        """
        SELECT id, downloader, external_id FROM tasks
        WHERE status='DOWNLOADING' AND (updated_at IS NULL OR updated_at < ?)
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        downloader = ctx.downloaders.get(row["downloader"])
        if downloader and row["external_id"]:
            try:
                downloader.cancel(row["external_id"])
            except Exception:
                pass
        conn.execute(
            """
            UPDATE tasks SET status='QUEUED', external_id=NULL, claimed_at=NULL,
                next_retry_at=?, updated_at=?
            WHERE id=? AND status='DOWNLOADING'
            """,
            (iso(now), iso(now), row["id"]),
        )
    conn.commit()
    return len(rows)
