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
from omnifetch.crawlers.base import CrawlBatch, CrawlError, CrawlInterrupted, CrawlerPlugin
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


def test_crawl_error_is_recorded_not_silently_successful(tmp_path):
    class _AllFailedCrawler(CrawlerPlugin):
        def __init__(self):
            super().__init__("broken", {"interval": 86400})

        def fetch(self):
            raise CrawlError("全部 2 个页面获取失败")

        def fetch_batches(self):
            yield CrawlBatch([], done=0, total=2)
            raise CrawlError("全部 2 个页面获取失败")

    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    ctx = runner.Context(
        Settings(db_path=str(tmp_path / "crawl.db"), config_dir=str(tmp_path / "cfg")),
        {"broken": _AllFailedCrawler()}, {}, [],
    )
    trigger.run_due_crawlers(conn, ctx, utcnow())
    ctx.wait_crawlers(5)

    row = conn.execute("SELECT * FROM source_runs").fetchone()
    assert row["finished_at"] is not None
    assert "CrawlError" in row["error"] and "全部 2 个页面获取失败" in row["error"]
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


def test_process_interrupted_run_resumes_immediately(tmp_path):
    # init_db marks a crashed run with exactly this error; restarting the
    # process is the retry, so do not sit out the failure backoff.
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO source_runs (source, started_at, error) VALUES ('slow', ?, ?)",
        (iso(now), "interrupted: process exited"),
    )
    conn.commit()

    assert trigger._is_due(conn, "slow", 86400, now)
    conn.close()


def test_crawl_creates_one_want_per_new_row_and_skips_old_rows(tmp_path):
    """VCB: every new sheet row becomes one want + one download; re-crawling
    the same rows must not add anything."""

    def row(uid: str) -> list[Resource]:
        title = f"Sheet Work {uid}"
        extra = {"vcb_uid": uid, "want_key": f"vcb-{uid}", "want_title": title}
        return [
            Resource(title=title, source_url=f"https://share.dmhy.org/topics/view/{uid}_x.html",
                     source="vcb-dmhy", download_uris=[f"magnet:?xt=urn:btih:{uid}"],
                     resource_type=ResourceType.MAGNET, extra=dict(extra)),
            Resource(title=title, source_url=f"https://nyaa.si/view/{uid}",
                     source="vcb-nyaa", download_uris=[f"https://nyaa.si/download/{uid}.torrent"],
                     resource_type=ResourceType.TORRENT, extra=dict(extra)),
        ]

    class _SheetCrawler(CrawlerPlugin):
        def __init__(self):
            super().__init__("sheet", {"interval": 86400})

        def fetch(self):
            return row("1001") + row("1002")

        def fetch_batches(self):
            yield CrawlBatch(self.fetch(), done=2, total=2)

    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    crawler = _SheetCrawler()
    ctx = runner.Context(
        Settings(db_path=str(tmp_path / "crawl.db"), config_dir=str(tmp_path / "cfg")),
        {"sheet": crawler}, {}, [],
    )

    trigger.run_crawler(ctx, "sheet", crawler, utcnow())
    assert conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM resources WHERE want_id IS NULL"
                        ).fetchone()[0] == 0

    # Same table again: both rows are duplicates, so no new wants/downloads.
    trigger.run_crawler(ctx, "sheet", crawler, utcnow())
    assert conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 4
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


def _add_failed_run(conn, source: str, started) -> None:
    conn.execute(
        "INSERT INTO source_runs (source, started_at, finished_at, error)"
        " VALUES (?, ?, ?, 'CrawlError: boom')",
        (source, iso(started), iso(started)),
    )
    conn.commit()


def test_failure_backoff_doubles_up_to_one_hour(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    now = utcnow()

    expected = [300, 600, 1200, 2400, 3600, 3600, 3600]
    for i, delay in enumerate(expected, start=1):
        _add_failed_run(conn, "vcb", now - timedelta(minutes=60 - i))
        assert trigger.failure_retry_delay(conn, "vcb") == delay

    # A later success resets the escalation.
    conn.execute(
        "INSERT INTO source_runs (source, started_at, finished_at)"
        " VALUES ('vcb', ?, ?)",
        (iso(now), iso(now)),
    )
    conn.commit()
    assert trigger.failure_retry_delay(conn, "vcb") == 300
    conn.close()


def test_failed_source_waits_for_its_backoff_before_retrying(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    now = utcnow()
    _add_failed_run(conn, "vcb", now - timedelta(minutes=50))
    _add_failed_run(conn, "vcb", now - timedelta(minutes=5))

    # Two consecutive failures -> 10 minutes, not the old flat 5.
    assert not trigger._is_due(conn, "vcb", 2592000, now)
    assert trigger._is_due(conn, "vcb", 2592000, now + timedelta(minutes=5))
    conn.close()
