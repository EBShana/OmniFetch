"""Task state machine. Every transition is an atomic conditional UPDATE:
the guard in the WHERE clause makes concurrent schedulers safe — a transition
that did not match exactly one row is a lost race and is reported as such.

Transitions:
  QUEUED --claim--> DOWNLOADING --result--> COMPLETED (terminal)
                     |        +--> FAILED_RETRYABLE --backoff expires--> QUEUED
                     +--> FAILED (terminal; fallback to next candidate is
                          decided by taskgen, not here)

Manual (Web UI): FAILED/IGNORED --retry--> QUEUED; FAILED* --ignore--> IGNORED.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .db import fetch_task, fetch_want, fetch_resource
from .models import (
    ErrorKind,
    Task,
    TaskStatus,
    WantStatus,
)
from .util import iso


@dataclass
class RetryPolicy:
    max_retries: int = 5
    base_seconds: int = 60
    max_seconds: int = 7200

    def backoff_seconds(self, retry_count: int) -> int:
        """Exponential backoff with a cap: 1min, 2min, 4min ... max 2h."""
        return min(self.base_seconds * (2 ** retry_count), self.max_seconds)


class FailOutcome(str, Enum):
    RETRY_SAME = "RETRY_SAME"   # waiting for backoff, same candidate retried
    FAILED = "FAILED"           # terminal failure recorded
    NOOP = "NOOP"               # task was not DOWNLOADING; nothing to do


def claim_task(conn: sqlite3.Connection, now: datetime) -> Task | None:
    """Atomically claim the oldest due task: plain QUEUED rows plus
    FAILED_RETRYABLE rows whose backoff has expired. None if none due."""
    stamp = iso(now)
    cur = conn.execute(
        """
        UPDATE tasks
        SET status='DOWNLOADING', claimed_at=?, last_progress_at=?,
            next_retry_at=NULL, updated_at=?
        WHERE id = (
                SELECT id FROM tasks
                WHERE (status='QUEUED' AND (next_retry_at IS NULL OR next_retry_at <= ?))
                   OR (status='FAILED_RETRYABLE' AND next_retry_at IS NOT NULL
                       AND next_retry_at <= ?)
                ORDER BY id LIMIT 1
            )
          AND status IN ('QUEUED', 'FAILED_RETRYABLE')
        RETURNING id
        """,
        (stamp, stamp, stamp, stamp, stamp),
    )
    row = cur.fetchone()
    conn.commit()
    if row is None:
        return None
    return fetch_task(conn, row["id"])


def begin_download(
    conn: sqlite3.Connection, task_id: int, external_id: str, now: datetime
) -> bool:
    """Record the downloader-side handle after a successful submit."""
    task = conn.execute("SELECT downloader FROM tasks WHERE id=?", (task_id,)).fetchone()
    cur = conn.execute(
        """
        UPDATE tasks SET external_id=?, dispatched_at=?, updated_at=?
        WHERE id=? AND status='DOWNLOADING'
        """,
        (external_id, iso(now), iso(now), task_id),
    )
    if cur.rowcount == 1 and task is not None:
        # Quota is per submission, not per task: a retry submitted later the
        # same day consumes another slot.
        conn.execute(
            "INSERT INTO dispatch_events (task_id, downloader, dispatched_at)"
            " VALUES (?, ?, ?)",
            (task_id, task["downloader"], iso(now)),
        )
    conn.commit()
    return cur.rowcount == 1


def retarget_task(
    conn: sqlite3.Connection, task_id: int, external_id: str, now: datetime
) -> None:
    """Update the tool-side handle when a downloader reports a new one
    (e.g. aria2 followedBy chain after a magnet fetch)."""
    conn.execute(
        "UPDATE tasks SET external_id=?, updated_at=? WHERE id=? AND status='DOWNLOADING'",
        (external_id, iso(now), task_id),
    )
    conn.commit()


def update_progress(
    conn: sqlite3.Connection, task_id: int, progress: float, now: datetime
) -> None:
    conn.execute(
        """
        UPDATE tasks SET progress=?, last_progress_at=?, updated_at=?
        WHERE id=? AND status='DOWNLOADING'
        """,
        (progress, iso(now), iso(now), task_id),
    )
    conn.commit()


def heartbeat(conn: sqlite3.Connection, task_id: int, now: datetime) -> None:
    """Refresh the liveness timestamp after a successful poll."""
    conn.execute(
        "UPDATE tasks SET updated_at=? WHERE id=? AND status='DOWNLOADING'",
        (iso(now), task_id),
    )
    conn.commit()


def complete_task(conn: sqlite3.Connection, task_id: int, now: datetime) -> str:
    """Mark DOWNLOADING -> COMPLETED. If this completes a want, converge it.

    Returns 'WANT_ACQUIRED' or 'OK'.
    """
    cur = conn.execute(
        """
        UPDATE tasks SET status='COMPLETED', progress=1, error_kind=NULL,
            error_message=NULL, updated_at=?
        WHERE id=? AND status='DOWNLOADING'
        """,
        (iso(now), task_id),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise RuntimeError(f"complete_task: task {task_id} is not DOWNLOADING")

    task = fetch_task(conn, task_id)
    resource = fetch_resource(conn, task.resource_id)
    if resource and resource.want_id:
        want = fetch_want(conn, resource.want_id)
        if want and want.status == WantStatus.WANTED:
            stamp = iso(now)
            conn.execute(
                "UPDATE wants SET status='ACQUIRED', acquired_resource_id=?, updated_at=? WHERE id=?",
                (resource.id, stamp, want.id),
            )
            conn.execute(
                "UPDATE resources SET status='TASKED', updated_at=? WHERE id=?",
                (stamp, resource.id),
            )
            conn.execute(
                """
                UPDATE resources SET status='REDUNDANT', updated_at=?
                WHERE want_id=? AND id<>? AND status<>'REDUNDANT'
                """,
                (stamp, want.id, resource.id),
            )
            conn.commit()
            return "WANT_ACQUIRED"
    return "OK"


def fail_task(
    conn: sqlite3.Connection,
    task_id: int,
    kind: ErrorKind,
    message: str,
    policy: RetryPolicy,
    now: datetime,
) -> FailOutcome:
    """Record a failure while DOWNLOADING. Decides retry vs terminal failure;
    candidate fallback itself is orchestrated by taskgen.on_task_failed."""
    task = fetch_task(conn, task_id)
    if task is None or task.status != TaskStatus.DOWNLOADING:
        return FailOutcome.NOOP  # already handled elsewhere; stay idempotent

    if kind == ErrorKind.TRANSIENT and task.retry_count < policy.max_retries:
        delay = policy.backoff_seconds(task.retry_count)
        next_retry = now + timedelta(seconds=delay)
        conn.execute(
            """
            UPDATE tasks SET status='FAILED_RETRYABLE', retry_count=retry_count+1,
                next_retry_at=?, progress=0, error_kind=?, error_message=?, updated_at=?
            WHERE id=? AND status='DOWNLOADING'
            """,
            (iso(next_retry), kind.value, message, iso(now), task_id),
        )
        conn.commit()
        return FailOutcome.RETRY_SAME

    conn.execute(
        """
        UPDATE tasks SET status='FAILED', error_kind=?, error_message=?, updated_at=?
        WHERE id=? AND status='DOWNLOADING'
        """,
        (kind.value, message, iso(now), task_id),
    )
    conn.commit()
    return FailOutcome.FAILED


def retry_task(conn: sqlite3.Connection, task_id: int, now: datetime) -> bool:
    """Manual retry (Web UI): FAILED/IGNORED/FAILED_RETRYABLE -> QUEUED.
    Revives a GIVEN_UP want back to WANTED so the chain can resume."""
    cur = conn.execute(
        """
        UPDATE tasks SET status='QUEUED', retry_count=0, next_retry_at=NULL,
            progress=0, error_kind=NULL, error_message=NULL, updated_at=?
        WHERE id=? AND status IN ('FAILED', 'IGNORED', 'FAILED_RETRYABLE')
        """,
        (iso(now), task_id),
    )
    if cur.rowcount == 1:
        row = conn.execute(
            "SELECT want_id FROM resources WHERE id=(SELECT resource_id FROM tasks WHERE id=?)",
            (task_id,),
        ).fetchone()
        if row and row["want_id"]:
            conn.execute(
                "UPDATE wants SET status='WANTED', updated_at=? WHERE id=? AND status='GIVEN_UP'",
                (iso(now), row["want_id"]),
            )
    conn.commit()
    return cur.rowcount == 1


def ignore_task(conn: sqlite3.Connection, task_id: int, now: datetime) -> bool:
    cur = conn.execute(
        """
        UPDATE tasks SET status='IGNORED', updated_at=?
        WHERE id=? AND status IN ('FAILED', 'FAILED_RETRYABLE', 'QUEUED')
        """,
        (iso(now), task_id),
    )
    conn.commit()
    return cur.rowcount == 1


def revive_want(conn: sqlite3.Connection, want_id: int, now: datetime) -> None:
    """Re-open a want (e.g. user wants a better version after acquiring).

    Candidates that were marked REDUNDANT when the want got acquired become
    available again; the acquired resource keeps its TASKED status so it is
    not downloaded twice."""
    stamp = iso(now)
    conn.execute(
        "UPDATE wants SET status='WANTED', acquired_resource_id=NULL, updated_at=? WHERE id=?",
        (stamp, want_id),
    )
    conn.execute(
        "UPDATE resources SET status='NEW', updated_at=? WHERE want_id=? AND status='REDUNDANT'",
        (stamp, want_id),
    )
    conn.commit()
