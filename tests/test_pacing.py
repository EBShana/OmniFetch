"""Crawl-then-consume-slowly mechanics: archived-resource backfill and the
global download concurrency cap."""
from __future__ import annotations

import pytest

from datetime import timedelta

from omnifetch.config import Settings
from omnifetch.core import ingest, statemachine, taskgen
from omnifetch.core.db import connect, fetch_task, init_db
from omnifetch.core.matcher import create_want
from omnifetch.core.models import Resource, ResourceType, TaskStatus
from omnifetch.core.util import iso, utcnow
from omnifetch.scheduler import poller
from omnifetch.scheduler.runner import Context

DL = lambda resource: "test-dl"  # noqa: E731


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "pace.db"))
    init_db(c)
    yield c
    c.close()


def _resource(url: str, title: str) -> Resource:
    return Resource(title=title, source_url=url, source="vcb-nyaa",
                    download_uris=[url], resource_type=ResourceType.TORRENT)


def test_backfill_attaches_archived_resources(conn):
    # Ingest happens BEFORE any want exists (monthly full-sweep archive).
    res, created = ingest.ingest_resource(conn, _resource("https://x.example/1", "Wistoria 1080p"))
    assert created and res.want_id is None

    want, matched = create_want(conn, "wistoria", match_rule={"aliases": ["wistoria"]})
    assert matched == 1
    row = conn.execute("SELECT want_id FROM resources WHERE id=?", (res.id,)).fetchone()
    assert row["want_id"] == want.id


def test_backfill_does_not_steal_other_wants_resources(conn):
    res, _ = ingest.ingest_resource(conn, _resource("https://x.example/1", "Wistoria 1080p"))
    want1, _ = create_want(conn, "wistoria", match_rule={"aliases": ["wistoria"]})
    want2, matched2 = create_want(conn, "other", match_rule={"aliases": ["wistoria"]})
    assert matched2 == 0  # already attached to want1
    row = conn.execute("SELECT want_id FROM resources WHERE id=?", (res.id,)).fetchone()
    assert row["want_id"] == want1.id != want2.id


class _InstantDownloader:
    def submit(self, resource):
        return "gid-x"

    def supports(self, resource):
        return True


def _two_queued_tasks(conn):
    task_ids = []
    for i in (1, 2):
        res, _ = ingest.ingest_resource(conn, _resource(f"https://x.example/{i}", f"Work {i} 1080p"))
        want, _ = create_want(conn, f"work {i}")
        conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want.id, res.id))
        conn.commit()
        taskgen.advance_want(conn, want.id, DL, utcnow())
        task_ids.append(conn.execute(
            "SELECT id FROM tasks WHERE resource_id=?", (res.id,)).fetchone()["id"])
    return task_ids


def test_dispatch_respects_concurrency_cap(conn):
    t1, t2 = _two_queued_tasks(conn)
    settings = Settings()
    settings.max_active_downloads = 1
    ctx = Context(settings=settings, crawlers={}, downloaders={"test-dl": _InstantDownloader()},
                  notifiers=[])

    poller.dispatch(conn, ctx, utcnow())
    assert fetch_task(conn, t1).status == TaskStatus.DOWNLOADING
    assert fetch_task(conn, t2).status == TaskStatus.QUEUED  # waits for a free slot

    statemachine.complete_task(conn, t1, utcnow())  # slot freed
    poller.dispatch(conn, ctx, utcnow())
    assert fetch_task(conn, t2).status == TaskStatus.DOWNLOADING


def test_dispatch_cap_two(conn):
    t1, t2 = _two_queued_tasks(conn)
    settings = Settings()  # default cap is 3
    ctx = Context(settings=settings, crawlers={}, downloaders={"test-dl": _InstantDownloader()},
                  notifiers=[])
    poller.dispatch(conn, ctx, utcnow())
    assert fetch_task(conn, t1).status == TaskStatus.DOWNLOADING
    assert fetch_task(conn, t2).status == TaskStatus.DOWNLOADING


def _queued_tasks(conn, count: int) -> list[int]:
    task_ids = []
    for i in range(count):
        res, _ = ingest.ingest_resource(
            conn, _resource(f"https://x.example/q{i}", f"Queued {i} 1080p"))
        want, _ = create_want(conn, f"queued {i}")
        conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want.id, res.id))
        conn.commit()
        taskgen.advance_want(conn, want.id, DL, utcnow())
        task_ids.append(conn.execute(
            "SELECT id FROM tasks WHERE resource_id=?", (res.id,)).fetchone()["id"])
    return task_ids


def _dispatch_ctx(**overrides) -> Context:
    settings = Settings()
    settings.max_active_downloads = 100   # keep concurrency out of the way
    settings.max_dispatch_per_tick = 100
    for key, value in overrides.items():
        setattr(settings, key, value)
    return Context(settings=settings, crawlers={},
                   downloaders={"test-dl": _InstantDownloader()}, notifiers=[])


def test_dispatch_respects_daily_limit(conn):
    ids = _queued_tasks(conn, 5)
    ctx = _dispatch_ctx(dispatch_daily_limit=2)
    now = utcnow()

    assert poller.dispatch(conn, ctx, now) == 2
    assert fetch_task(conn, ids[0]).status == TaskStatus.DOWNLOADING
    assert fetch_task(conn, ids[1]).status == TaskStatus.DOWNLOADING

    # Quota exhausted: nothing else leaves the queue today.
    assert poller.dispatch(conn, ctx, now) == 0
    assert fetch_task(conn, ids[2]).status == TaskStatus.QUEUED


def test_default_daily_limit_is_fifty(conn):
    ids = _queued_tasks(conn, 51)
    ctx = _dispatch_ctx()  # Settings default

    assert poller.dispatch(conn, ctx, utcnow()) == 50
    assert fetch_task(conn, ids[50]).status == TaskStatus.QUEUED


def test_daily_limit_resets_after_local_midnight(conn):
    ids = _queued_tasks(conn, 4)
    ctx = _dispatch_ctx(dispatch_daily_limit=2)
    now = utcnow()
    assert poller.dispatch(conn, ctx, now) == 2

    # Those two were submitted "yesterday": they no longer consume today's quota.
    yesterday = iso(now - timedelta(days=1))
    conn.execute("UPDATE tasks SET dispatched_at=? WHERE id IN (?, ?)",
                 (yesterday, ids[0], ids[1]))
    conn.commit()
    statemachine.complete_task(conn, ids[0], now)
    statemachine.complete_task(conn, ids[1], now)

    assert poller.dispatch(conn, ctx, now) == 2
    assert fetch_task(conn, ids[2]).status == TaskStatus.DOWNLOADING
    assert fetch_task(conn, ids[3]).status == TaskStatus.DOWNLOADING


def test_zero_daily_limit_means_unlimited(conn):
    ids = _queued_tasks(conn, 3)
    ctx = _dispatch_ctx(dispatch_daily_limit=0)

    assert poller.dispatch(conn, ctx, utcnow()) == 3
    assert all(fetch_task(conn, i).status == TaskStatus.DOWNLOADING for i in ids)
