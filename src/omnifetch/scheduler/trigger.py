"""Crawl trigger: run each due source in a background worker thread.

A long crawl (VCB's sheet has ~1100 rows) must never block the scheduler
tick, so run_due_crawlers only starts workers. Each worker opens its own DB
connection, inserts a source_runs row immediately (visible as "running"),
ingests every batch as it arrives, and updates done/total progress until it
finishes or is interrupted. A crashing crawler must never break the loop or
lose previously ingested batches — errors are recorded and skipped.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from typing import TYPE_CHECKING

from ..core import ingest
from ..core.util import iso, parse_iso, utcnow
from ..crawlers.base import CrawlInterrupted

if TYPE_CHECKING:
    from .runner import Context

log = logging.getLogger(__name__)

# After a failed run, retry sooner than the source's own interval so a
# monthly source does not stay broken for a month because of one bad tick.
_FAILURE_RETRY_SECONDS = 300


def run_due_crawlers(conn: sqlite3.Connection, ctx: "Context", now: datetime) -> None:
    """Start due sources without waiting for them. Returns immediately."""
    for name, crawler in list(ctx.crawlers.items()):
        interval = int(crawler.config.get("interval", 900))
        if ctx.is_crawler_running(name):
            continue
        # Schedule is persisted in source_runs, so container restarts do NOT
        # reset it — a monthly source stays monthly across deploys.
        if not _is_due(conn, name, interval, now):
            continue
        ctx.start_crawler(name, crawler, now)


def run_crawler(ctx: "Context", name: str, crawler, now: datetime) -> None:
    """Worker body: ingest batches incrementally and persist progress."""
    try:
        conn = ctx.conn()
    except Exception:
        log.exception("crawler %s: cannot open database", name)
        ctx.finish_crawler(name)
        return

    started = time.monotonic()
    run_id = None
    fetched, created, duplicates, done, total = 0, 0, 0, 0, 0
    error = None
    try:
        run_id = _start_run(conn, name, now)
        for batch in crawler.fetch_batches():
            resources = list(batch.resources)
            result = ingest.ingest_resources(conn, resources)
            fetched += len(resources)
            created += result.created
            duplicates += result.duplicates
            if batch.done:
                done = batch.done
            if batch.total:
                total = batch.total
            _update_run(conn, run_id, fetched, created, duplicates, done, total,
                        int((time.monotonic() - started) * 1000))
    except CrawlInterrupted as exc:
        log.info("crawler %s interrupted after %d/%d rows", name, done, total)
        error = f"interrupted: {exc}"[:500]
    except Exception as exc:
        log.exception("crawler %s failed; keeping ingested batches", name)
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        elapsed = int((time.monotonic() - started) * 1000)
        if run_id is not None:
            try:
                _finish_run(conn, run_id, fetched, created, duplicates,
                            done, total, elapsed, error)
            except Exception:
                log.exception("crawler %s: cannot write final run row", name)
        conn.close()
        ctx.finish_crawler(name)
    log.info(
        "crawler %s: %d fetched, %d new, %d duplicates, %d/%d rows%s",
        name, fetched, created, duplicates, done, total,
        f", error={error}" if error else "",
    )


def _is_due(conn: sqlite3.Connection, name: str, interval: int, now: datetime) -> bool:
    row = conn.execute(
        """
        SELECT started_at, error, finished_at FROM source_runs
        WHERE source=? ORDER BY id DESC LIMIT 1
        """,
        (name,),
    ).fetchone()
    if row is None:
        return True
    last = parse_iso(row["started_at"])
    if last is None:
        return True
    # Unfinished rows mean the process died mid-crawl (a live run is filtered
    # out by is_crawler_running before we get here), so retry soon.
    unfinished = row["finished_at"] is None
    wait = min(interval, _FAILURE_RETRY_SECONDS) if (row["error"] or unfinished) else interval
    return (now - last).total_seconds() >= wait


def _start_run(conn: sqlite3.Connection, source: str, now: datetime) -> int:
    cur = conn.execute(
        """
        INSERT INTO source_runs
            (source, started_at, fetched, created, duplicates, duration_ms, done, total)
        VALUES (?, ?, 0, 0, 0, 0, 0, 0)
        """,
        (source, iso(now)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _update_run(conn, run_id, fetched, created, duplicates, done, total, duration_ms):
    conn.execute(
        """
        UPDATE source_runs
        SET fetched=?, created=?, duplicates=?, done=?, total=?, duration_ms=?
        WHERE id=?
        """,
        (fetched, created, duplicates, done, total, duration_ms, run_id),
    )
    conn.commit()


def _finish_run(conn, run_id, fetched, created, duplicates, done, total,
                duration_ms, error):
    conn.execute(
        """
        UPDATE source_runs
        SET fetched=?, created=?, duplicates=?, done=?, total=?,
            duration_ms=?, finished_at=?, error=?
        WHERE id=?
        """,
        (fetched, created, duplicates, done, total, duration_ms,
         iso(utcnow()), error, run_id),
    )
    conn.commit()
