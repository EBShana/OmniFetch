"""Additional core tests: stalled-download detection + source preference ranking."""
from __future__ import annotations

from datetime import timedelta

from omnifetch.config import Settings
from omnifetch.core import ingest, statemachine, taskgen
from omnifetch.core.db import connect, fetch_task, init_db
from omnifetch.core.matcher import find_or_create_want
from omnifetch.core.models import Resource, ResourceType, TaskStatus
from omnifetch.core.taskgen import score
from omnifetch.core.util import iso, utcnow
from omnifetch.downloaders.base import PollStatus, TaskResult
from omnifetch.scheduler import poller
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

    def poll(self, external_id):
        return TaskResult(PollStatus.PROGRESS, progress=0.0)


def test_poller_marks_stalled_task_as_transient_failure(tmp_path):
    conn = _conn(tmp_path)
    _, want_id = _want_with_resource(conn)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    statemachine.begin_download(conn, task.id, "gid-x", now)

    # Simulate 20 minutes passing without any progress.
    old_claim = iso(now - timedelta(minutes=20))
    conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (old_claim, task.id))
    conn.commit()

    settings = Settings()
    settings.stalled_after = timedelta(minutes=15)
    ctx = Context(settings=settings, crawlers={}, downloaders={"test-dl": _FrozenDownloader()},
                  notifiers=[])
    poller.poll_active(conn, ctx, utcnow())

    after = fetch_task(conn, task.id)
    assert after.status == TaskStatus.FAILED_RETRYABLE
    assert "stalled" in (after.error_message or "")


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
