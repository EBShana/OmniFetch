"""Additional core tests: stalled-download detection + source preference ranking."""
from __future__ import annotations

from datetime import timedelta

from omnifetch.config import Settings
from omnifetch.core import ingest, statemachine, taskgen
from omnifetch.core.db import connect, fetch_task, init_db
from omnifetch.core.matcher import find_or_create_want
from omnifetch.core.models import ErrorKind, Resource, ResourceType, TaskStatus
from omnifetch.core.taskgen import score
from omnifetch.core.util import iso, utcnow
from omnifetch.downloaders.base import PollStatus, TaskResult
from omnifetch.scheduler import poller
from omnifetch.scheduler.reaper import sweep
from omnifetch.scheduler.runner import Context

DL = lambda resource: "test-dl"  # noqa: E731


def _conn(tmp_path):
    conn = connect(str(tmp_path / "stall.db"))
    init_db(conn)
    return conn


def _want_with_resource(conn, url="https://a.example/1"):
    res, _ = ingest.ingest_resource(
        conn,
        Resource(title="Work 1080p", source_url=url, source="test",
                 download_uris=[url], resource_type=ResourceType.HTTP),
    )
    want = find_or_create_want(conn, "work")
    conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want.id, res.id))
    conn.commit()
    return res, want.id


class _FrozenDownloader:
    """Always reports PROGRESS with zero movement."""

    def __init__(self):
        self.cancelled: list[str] = []

    def poll(self, external_id):
        return TaskResult(PollStatus.PROGRESS, progress=0.0)

    def cancel(self, external_id):
        self.cancelled.append(external_id)


def test_poller_marks_stalled_task_as_transient_failure(tmp_path):
    conn = _conn(tmp_path)
    _, want_id = _want_with_resource(conn)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    statemachine.begin_download(conn, task.id, "gid-x", now)

    # Simulate 20 minutes passing without any progress.
    old = iso(now - timedelta(minutes=20))
    conn.execute("UPDATE tasks SET claimed_at=?, last_progress_at=? WHERE id=?",
                 (old, old, task.id))
    conn.commit()

    settings = Settings()
    settings.stalled_after = timedelta(minutes=15)
    downloader = _FrozenDownloader()
    ctx = Context(settings=settings, crawlers={}, downloaders={"test-dl": downloader},
                  notifiers=[])
    poller.poll_active(conn, ctx, utcnow())

    after = fetch_task(conn, task.id)
    assert after.status == TaskStatus.FAILED_RETRYABLE
    assert "stalled" in (after.error_message or "")
    assert downloader.cancelled == ["gid-x"]


def test_poller_stall_uses_last_progress_not_claim_time(tmp_path):
    conn = _conn(tmp_path)
    _, want_id = _want_with_resource(conn)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    statemachine.begin_download(conn, task.id, "gid-x", now)
    statemachine.update_progress(conn, task.id, 0.5, now)

    # Claimed long ago, but progress was recorded recently -> not stalled.
    conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?",
                 (iso(now - timedelta(minutes=30)), task.id))
    conn.commit()

    settings = Settings()
    settings.stalled_after = timedelta(minutes=15)
    ctx = Context(settings=settings, crawlers={},
                  downloaders={"test-dl": _FrozenDownloader()}, notifiers=[])
    poller.poll_active(conn, ctx, utcnow())

    assert fetch_task(conn, task.id).status == TaskStatus.DOWNLOADING


def test_poller_does_not_stall_healthy_task(tmp_path):
    conn = _conn(tmp_path)
    _, want_id = _want_with_resource(conn)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    statemachine.begin_download(conn, task.id, "gid-x", now)
    statemachine.update_progress(conn, task.id, 0.5, now)  # moving forward

    settings = Settings()
    settings.stalled_after = timedelta(minutes=15)
    ctx = Context(settings=settings, crawlers={}, downloaders={"test-dl": _FrozenDownloader()},
                  notifiers=[])
    poller.poll_active(conn, ctx, utcnow())

    assert fetch_task(conn, task.id).status == TaskStatus.DOWNLOADING


def test_score_prefer_sources():
    dmhy = Resource(title="T", source_url="u1", source="vcb-dmhy",
                    download_uris=["u"], resource_type=ResourceType.HTTP)
    nyaa = Resource(title="T", source_url="u2", source="vcb-nyaa",
                    download_uris=["u"], resource_type=ResourceType.HTTP)
    rule = {"prefer_sources": ["vcb-nyaa"]}
    assert score(nyaa, rule) > score(dmhy, rule)
    assert score(nyaa, {}) == score(dmhy, {})  # without rule: tie, id order decides


def test_reaper_uses_updated_at_heartbeat(tmp_path):
    conn = _conn(tmp_path)
    _, want_id = _want_with_resource(conn)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    statemachine.begin_download(conn, task.id, "gid-x", now)

    old = iso(now - timedelta(minutes=30))
    # Old claim time is irrelevant as long as a heartbeat arrived recently.
    conn.execute("UPDATE tasks SET claimed_at=?, updated_at=? WHERE id=?",
                 (old, iso(now), task.id))
    conn.commit()

    settings = Settings()
    settings.reaper_stale = timedelta(minutes=10)
    ctx = Context(settings=settings, crawlers={}, downloaders={}, notifiers=[])
    assert sweep(conn, ctx, now) == 0
    assert fetch_task(conn, task.id).status == TaskStatus.DOWNLOADING

    # No heartbeat for 30 minutes -> zombie.
    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old, task.id))
    conn.commit()
    assert sweep(conn, ctx, now) == 1
    assert fetch_task(conn, task.id).status == TaskStatus.QUEUED


def test_poller_refreshes_heartbeat(tmp_path):
    conn = _conn(tmp_path)
    _, want_id = _want_with_resource(conn)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    statemachine.begin_download(conn, task.id, "gid-x", now)

    old = iso(now - timedelta(minutes=30))
    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old, task.id))
    conn.commit()

    settings = Settings()
    ctx = Context(settings=settings, crawlers={}, downloaders={"test-dl": _FrozenDownloader()},
                  notifiers=[])
    poller.poll_active(conn, ctx, now)

    row = conn.execute("SELECT updated_at FROM tasks WHERE id=?", (task.id,)).fetchone()
    assert row["updated_at"] > iso(now - timedelta(minutes=1))


class _FailingDownloader:
    def poll(self, external_id):
        return TaskResult(PollStatus.FAILED, error_kind=ErrorKind.TRANSIENT,
                          error_message="boom")

    def cancel(self, external_id):
        pass


def test_failed_branch_does_not_fail_twice(tmp_path):
    """FAILED must not fall through into the stall check and double-retry."""
    conn = _conn(tmp_path)
    _, want_id = _want_with_resource(conn)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    statemachine.begin_download(conn, task.id, "gid-x", now)

    conn.execute("UPDATE tasks SET last_progress_at=? WHERE id=?",
                 (iso(now - timedelta(minutes=30)), task.id))
    conn.commit()

    settings = Settings()
    settings.stalled_after = timedelta(minutes=15)
    ctx = Context(settings=settings, crawlers={},
                  downloaders={"test-dl": _FailingDownloader()}, notifiers=[])
    poller.poll_active(conn, ctx, now)

    after = fetch_task(conn, task.id)
    assert after.status == TaskStatus.FAILED_RETRYABLE
    assert after.retry_count == 1
