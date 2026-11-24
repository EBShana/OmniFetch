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


def run_due_crawlers(conn: sqlite3.Connection, ctx: "Context", now: datetime) -> None:
    for name, crawler in ctx.crawlers.items():
        interval = int(crawler.config.get("interval", 900))
        # Schedule is persisted in source_runs, so container restarts do NOT
        # reset it — a monthly source stays monthly across deploys.
        last_row = conn.execute(
            "SELECT MAX(started_at) FROM source_runs WHERE source=?", (name,)
        ).fetchone()
        last = parse_iso(last_row[0]) if last_row and last_row[0] else None
        if last is not None and (now - last).total_seconds() < interval:
            continue
        ctx.last_runs[name] = now

        started = time.monotonic()
        fetched, created, duplicates, error = 0, 0, 0, None
        try:
            found = crawler.fetch()
            fetched = len(found)
            result = ingest.ingest_resources(conn, found)
            created, duplicates = result.created, result.duplicates
        except Exception:
            log.exception("crawler %s failed; skipping this round", name)
            error = "fetch failed"
        _record_run(conn, name, now, fetched, created, duplicates,
                    int((time.monotonic() - started) * 1000), error)
        log.info(
            "crawler %s: %d fetched, %d new, %d duplicates",
            name, fetched, created, duplicates,
        )


def _record_run(conn, source, now, fetched, created, duplicates, duration_ms, error):
    conn.execute(
        """
        INSERT INTO source_runs (source, started_at, fetched, created, duplicates, duration_ms, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (source, iso(now), fetched, created, duplicates, duration_ms, error),
    )
    conn.commit()
