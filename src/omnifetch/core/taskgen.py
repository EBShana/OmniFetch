"""Candidate ranking and the per-want fallback chain.

Policy (mirrors the architecture discussion):
  - transient failure                       -> retry the SAME candidate with
                                               exponential backoff (statemachine)
  - permanent failure / retries exhausted   -> advance to the next-ranked
                                               untried candidate
  - no untried candidates left              -> want becomes GIVEN_UP, notify

One want never runs two chains at once: advance_want refuses to create a task
while any candidate of the want has a task in QUEUED/DOWNLOADING/FAILED_RETRYABLE.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from . import statemachine
from .db import fetch_task, fetch_want, resource_from_row
from .models import ErrorKind, Resource, WantStatus
from .statemachine import RetryPolicy
from .util import iso

# Resolve which downloader (its config name) should handle a resource.
DownloaderFor = Callable[[Resource], str | None]


def score(resource: Resource, rank_rule: dict) -> tuple:
    """Higher is better. Default order: preferred group, resolution, size."""
    q = resource.quality
    groups = [g.lower() for g in (rank_rule.get("prefer_groups") or [])]
    group_bonus = 1 if (q.group and q.group.lower() in groups) else 0
    size = int(resource.extra.get("size") or 0)
    return (group_bonus, q.resolution or 0, size)


def _want_has_active_chain(conn: sqlite3.Connection, want_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM tasks t JOIN resources r ON r.id = t.resource_id
        WHERE r.want_id = ? AND t.status IN ('QUEUED', 'DOWNLOADING', 'FAILED_RETRYABLE')
        LIMIT 1
        """,
        (want_id,),
    ).fetchone()
    return row is not None


def _untried_candidates(conn: sqlite3.Connection, want_id: int) -> list[Resource]:
    rows = conn.execute(
        "SELECT * FROM resources WHERE want_id=? AND status <> 'REDUNDANT' ORDER BY id",
        (want_id,),
    ).fetchall()
    candidates = []
    for row in rows:
        tried = conn.execute(
            "SELECT 1 FROM tasks WHERE resource_id=? LIMIT 1", (row["id"],)
        ).fetchone()
        if tried is None:
            candidates.append(resource_from_row(row))
    return candidates


def advance_want(
    conn: sqlite3.Connection,
    want_id: int,
    downloader_for: DownloaderFor,
    now: datetime,
) -> str:
    """Push the fallback chain of one want by one step.

    Returns 'NEW_TASK' | 'ACTIVE' | 'GIVE_UP' | 'NO_DOWNLOADER' | 'INACTIVE'.
    """
    want = fetch_want(conn, want_id)
    if want is None or want.status != WantStatus.WANTED:
        return "INACTIVE"
    if _want_has_active_chain(conn, want_id):
        return "ACTIVE"

    candidates = _untried_candidates(conn, want_id)
    if not candidates:
        conn.execute(
            "UPDATE wants SET status='GIVEN_UP', updated_at=? WHERE id=?",
            (iso(now), want_id),
        )
        conn.commit()
        return "GIVE_UP"

    best = max(candidates, key=lambda r: score(r, want.rank_rule))
    downloader = downloader_for(best)
    if downloader is None:
        conn.execute(
            "UPDATE wants SET status='GIVEN_UP', updated_at=? WHERE id=?",
            (iso(now), want_id),
        )
        conn.commit()
        return "NO_DOWNLOADER"

    stamp = iso(now)
    conn.execute(
        """
        INSERT INTO tasks (resource_id, downloader, status, created_at, updated_at)
        VALUES (?, ?, 'QUEUED', ?, ?)
        ON CONFLICT (resource_id, downloader) DO NOTHING
        """,
        (best.id, downloader, stamp, stamp),
    )
    conn.execute(
        "UPDATE resources SET status='TASKED', updated_at=? WHERE id=?", (stamp, best.id)
    )
    conn.commit()
    return "NEW_TASK"


def generate_all(
    conn: sqlite3.Connection, downloader_for: DownloaderFor, now: datetime
) -> list[tuple[int, str]]:
    """Advance every WANTED want that has no active chain. Returns
    [(want_id, outcome), ...] so the caller can emit notifications."""
    rows = conn.execute(
        "SELECT id FROM wants WHERE status=?", (WantStatus.WANTED.value,)
    ).fetchall()
    results = []
    for row in rows:
        outcome = advance_want(conn, row["id"], downloader_for, now)
        if outcome != "ACTIVE":
            results.append((row["id"], outcome))
    return results


def on_task_failed(
    conn: sqlite3.Connection,
    task_id: int,
    kind: ErrorKind,
    message: str,
    policy: RetryPolicy,
    downloader_for: DownloaderFor,
    now: datetime,
) -> str | None:
    """Entry point used by the poller when a download reports failure.

    Transient failures stay on the same candidate (backoff handled inside
    fail_task); only a terminal failure advances the fallback chain.
    Returns the advance outcome ('NEW_TASK'/'GIVE_UP'/...) or None if the
    task is merely retrying.
    """
    outcome = statemachine.fail_task(conn, task_id, kind, message, policy, now)
    if outcome != statemachine.FailOutcome.FAILED:
        return None
    task = fetch_task(conn, task_id)
    if task is None:
        return None
    row = conn.execute(
        "SELECT want_id FROM resources WHERE id=?", (task.resource_id,)
    ).fetchone()
    if row is None or row["want_id"] is None:
        return None
    return advance_want(conn, row["want_id"], downloader_for, now)
