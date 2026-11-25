"""Background/batched crawl execution: the scheduler tick must never wait
for a long crawl, and each batch must be durable before the next one starts.
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from omnifetch.config import Settings
from omnifetch.core.db import connect, init_db
from omnifetch.core.models import Resource, ResourceType
from omnifetch.core.util import iso, utcnow
from omnifetch.crawlers.base import CrawlBatch, CrawlInterrupted, CrawlerPlugin
from omnifetch.scheduler import runner, trigger


def _resource(n: int) -> Resource:
    url = f"https://fake.example/{n}"
    return Resource(title=f"Fake Work {n} 1080p", source_url=url, source="fake",
                    download_uris=[url], resource_type=ResourceType.HTTP)


class _BlockingCrawler(CrawlerPlugin):
    """Yields batch 1, then blocks until released or stopped."""

    def __init__(self, interval: int = 86400):
        super().__init__("slow", {"interval": interval})
        self.batch1_done = threading.Event()
        self.release = threading.Event()
        self.interrupted = False

    def fetch(self):
        return []

    def fetch_batches(self):
        yield CrawlBatch([_resource(1)], done=1, total=2)
        self.batch1_done.set()
        while not self.release.is_set():
            if self.should_stop():
                self.interrupted = True
                raise CrawlInterrupted("stopped by test")
            time.sleep(0.01)
        yield CrawlBatch([_resource(2)], done=2, total=2)


def _ctx(tmp_path, crawler) -> runner.Context:
    settings = Settings(db_path=str(tmp_path / "crawl.db"),
                        config_dir=str(tmp_path / "cfg"))
    return runner.Context(settings, {"slow": crawler}, {}, [])


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_scheduler_tick_returns_before_crawl_finishes(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    crawler = _BlockingCrawler()
    ctx = _ctx(tmp_path, crawler)

    started = time.monotonic()
    trigger.run_due_crawlers(conn, ctx, utcnow())
    assert time.monotonic() - started < 1.0     # not blocked by the crawl
    assert ctx.is_crawler_running("slow")

    try:
        # Batch 1 is ingested and the run row shows partial progress while the
        # crawler still holds batch 2.
        assert _wait_for(lambda: _count(conn) == 1)
        row = conn.execute("SELECT * FROM source_runs").fetchone()
        assert row["done"] == 1 and row["total"] == 2
        assert row["finished_at"] is None
        assert row["error"] is None

        crawler.release.set()
        ctx.wait_crawlers(5)
        assert _count(conn) == 2
        row = conn.execute("SELECT * FROM source_runs").fetchone()
        assert row["done"] == 2 and row["total"] == 2
        assert row["finished_at"] is not None
        assert row["error"] is None
    finally:
        if crawler.release.is_set() is False:
            ctx.stop_crawlers(5)
        conn.close()


def test_stop_interrupts_crawl_and_keeps_ingested_batch(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    crawler = _BlockingCrawler()
    ctx = _ctx(tmp_path, crawler)

    trigger.run_due_crawlers(conn, ctx, utcnow())
    assert crawler.batch1_done.wait(5)
    ctx.stop_crawlers(5)

    assert crawler.interrupted
    assert _count(conn) == 1                     # first batch was kept
    row = conn.execute("SELECT * FROM source_runs").fetchone()
    assert row["finished_at"] is not None
    assert row["error"].startswith("interrupted")
    conn.close()


def test_unfinished_run_is_retried_after_restart(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO source_runs (source, started_at) VALUES ('slow', ?)",
        (iso(now - timedelta(minutes=10)),),
    )
    conn.execute(
        "INSERT INTO source_runs (source, started_at) VALUES ('fresh', ?)",
        (iso(now),),
    )
    conn.commit()

    assert trigger._is_due(conn, "slow", 86400, now)       # stale -> retry
    assert not trigger._is_due(conn, "fresh", 86400, now)  # running/just died
    conn.close()


def test_successful_run_keeps_full_interval(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO source_runs (source, started_at, finished_at)
        VALUES ('slow', ?, ?)
        """,
        (iso(now - timedelta(minutes=10)), iso(now - timedelta(minutes=9))),
    )
    conn.commit()

    assert not trigger._is_due(conn, "slow", 3600, now)
    assert trigger._is_due(conn, "slow", 3600, now + timedelta(seconds=3601))
    conn.close()
