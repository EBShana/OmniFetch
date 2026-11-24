"""Crawl-then-consume-slowly mechanics: archived-resource backfill and the
global download concurrency cap."""
from __future__ import annotations

import pytest

from omnifetch.config import Settings
from omnifetch.core import ingest, statemachine, taskgen
from omnifetch.core.db import connect, fetch_task, init_db
from omnifetch.core.matcher import create_want
from omnifetch.core.models import Resource, ResourceType, TaskStatus
from omnifetch.core.util import utcnow
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
