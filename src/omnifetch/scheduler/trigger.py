"""Crawl trigger: run each enabled source whose interval has elapsed and
ingest the results. A crashing crawler must never break the loop or lose
previously ingested data — errors are logged and skipped."""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from typing import TYPE_CHECKING

from ..core import ingest
from ..core.util import iso, parse_iso, utcnow

if TYPE_CHECKING:
    from .runner import Context

log = logging.getLogger(__name__)

# After a failed run, retry sooner than the source's own interval so a
# monthly source does not stay broken for a month because of one bad tick.
_FAILURE_RETRY_SECONDS = 300


def run_due_crawlers(conn: sqlite3.Connection, ctx: "Context", now: datetime) -> None:
    for name, crawler in ctx.crawlers.items():
        interval = int(crawler.config.get("interval", 900))
        # Schedule is persisted in source_runs, so container restarts do NOT
        # reset it — a monthly source stays monthly across deploys.
        if not _is_due(conn, name, interval, now):
            continue
        ctx.last_runs[name] = now

        started = time.monotonic()
        fetched, created, duplicates, error = 0, 0, 0, None
        try:
            found = crawler.fetch()
            fetched = len(found)
            result = ingest.ingest_resources(conn, found)
            created, duplicates = result.created, result.duplicates
        except Exception as exc:
            log.exception("crawler %s failed; skipping this round", name)
            error = f"{type(exc).__name__}: {exc}"[:500]
        _record_run(conn, name, now, fetched, created, duplicates,
                    int((time.monotonic() - started) * 1000), error)
        log.info(
            "crawler %s: %d fetched, %d new, %d duplicates",
            name, fetched, created, duplicates,
        )


def _is_due(conn: sqlite3.Connection, name: str, interval: int, now: datetime) -> bool:
    row = conn.execute(
        "SELECT started_at, error FROM source_runs WHERE source=? ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return True
    last = parse_iso(row["started_at"])
    if last is None:
        return True
    wait = min(interval, _FAILURE_RETRY_SECONDS) if row["error"] else interval
    return (now - last).total_seconds() >= wait


def _record_run(conn, source, now, fetched, created, duplicates, duration_ms, error):
    conn.execute(
        """
        INSERT INTO source_runs (source, started_at, fetched, created, duplicates, duration_ms, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (source, iso(now), fetched, created, duplicates, duration_ms, error),
    )
    conn.commit()
