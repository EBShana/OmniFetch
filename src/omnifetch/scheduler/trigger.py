"""Crawl trigger: run each enabled source whose interval has elapsed and
ingest the results. A crashing crawler must never break the loop or lose
previously ingested data — errors are logged and skipped."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

from ..core import ingest

if TYPE_CHECKING:
    from .runner import Context

log = logging.getLogger(__name__)


def run_due_crawlers(conn: sqlite3.Connection, ctx: "Context", now: datetime) -> None:
    for name, crawler in ctx.crawlers.items():
        interval = int(crawler.config.get("interval", 900))
        last = ctx.last_runs.get(name)
        if last is not None and (now - last).total_seconds() < interval:
            continue
        ctx.last_runs[name] = now
        try:
            found = crawler.fetch()
        except Exception:
            log.exception("crawler %s failed; skipping this round", name)
            continue
        result = ingest.ingest_resources(conn, found)
        log.info(
            "crawler %s: %d fetched, %d new, %d duplicates",
            name, len(found), result.created, result.duplicates,
        )
