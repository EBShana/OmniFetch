"""Core mechanics: dedup, atomic claim, retry/backoff, fallback chain."""
from __future__ import annotations

from datetime import timedelta

import pytest

from omnifetch.core import ingest, statemachine, taskgen
from omnifetch.core.db import connect, fetch_task, fetch_want, init_db
from omnifetch.core.matcher import find_or_create_want
from omnifetch.core.models import ErrorKind, Resource, ResourceType, WantStatus
from omnifetch.core.statemachine import RetryPolicy
from omnifetch.core.util import iso, utcnow

POLICY = RetryPolicy(max_retries=2, base_seconds=60, max_seconds=7200)
DL = lambda resource: "test-dl"  # noqa: E731


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_db(c)
    yield c
    c.close()


def make_resource(url: str, title: str, size: int | None = None) -> Resource:
    extra = {"size": size} if size else {}
    return Resource(
        title=title,
        source_url=url,
        source="test",
        download_uris=[url],
        resource_type=ResourceType.HTTP,
        extra=extra,
    )


def attach(conn, resource: Resource, work_key: str = "test work") -> int:
    want = find_or_create_want(conn, work_key)
    conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want.id, resource.id))
    conn.commit()
    return want.id


# --------------------------------------------------------------------- dedup

def test_same_url_ingested_once(conn):
    r1, created1 = ingest.ingest_resource(conn, make_resource("https://a.example/x", "Title 1080p"))
    r2, created2 = ingest.ingest_resource(conn, make_resource("https://a.example/x", "Title 1080p"))
    assert created1 and not created2
    assert r1.id == r2.id


def test_tracking_params_do_not_change_identity(conn):
    r1, _ = ingest.ingest_resource(conn, make_resource("https://a.example/x", "Title"))
    r2, created = ingest.ingest_resource(
        conn, make_resource("https://a.example/x?utm_source=feed&ref=1", "Title")
    )
    assert not created and r1.id == r2.id


def test_same_version_on_other_site_merges_into_one_candidate(conn):
    title = "Some Release 2024 1080p BluRay"
    _, created1 = ingest.ingest_resource(conn, make_resource("https://a.example/1", title, size=5000))
    _, created2 = ingest.ingest_resource(conn, make_resource("https://b.example/2", title, size=5000))
    assert created1 and not created2


# ----------------------------------------------------------- claim + retry

def test_claim_is_atomic_and_respects_backoff(conn):
    res, _ = ingest.ingest_resource(conn, make_resource("https://a.example/1", "Work 1080p"))
    want_id = attach(conn, res)
    now = utcnow()

    assert taskgen.advance_want(conn, want_id, DL, now) == "NEW_TASK"
    task = statemachine.claim_task(conn, now)
    assert task is not None and task.status.value == "DOWNLOADING"
    assert statemachine.claim_task(conn, now) is None  # nothing else queued

    outcome = statemachine.fail_task(conn, task.id, ErrorKind.TRANSIENT, "timeout", POLICY, now)
    assert outcome == statemachine.FailOutcome.RETRY_SAME
    task = fetch_task(conn, task.id)
    assert task.status.value == "FAILED_RETRYABLE"
    assert task.retry_count == 1
    assert task.next_retry_at is not None

    assert statemachine.claim_task(conn, now) is None  # backoff not expired
    past = iso(utcnow() - timedelta(seconds=5))
    conn.execute("UPDATE tasks SET next_retry_at=? WHERE id=?", (past, task.id))
    conn.commit()
    assert statemachine.claim_task(conn, utcnow()) is not None


def test_retries_exhausted_then_given_up(conn):
    res, _ = ingest.ingest_resource(conn, make_resource("https://a.example/1", "Work 1080p"))
    want_id = attach(conn, res)
    now = utcnow()

    taskgen.advance_want(conn, want_id, DL, now)
    for _ in range(POLICY.max_retries):
        task = statemachine.claim_task(conn, utcnow())
        statemachine.fail_task(conn, task.id, ErrorKind.TRANSIENT, "timeout", POLICY, utcnow())
        conn.execute("UPDATE tasks SET next_retry_at=? WHERE id=?",
                     (iso(utcnow() - timedelta(seconds=1)), task.id))
        conn.commit()
    # next failure exhausts retries -> terminal FAILED -> no candidates left
    task = statemachine.claim_task(conn, utcnow())
    outcome = taskgen.on_task_failed(
        conn, task.id, ErrorKind.TRANSIENT, "timeout", POLICY, DL, utcnow()
    )
    assert outcome == "GIVE_UP"
    assert fetch_want(conn, want_id).status == WantStatus.GIVEN_UP


# --------------------------------------------------------------- fallback

def test_permanent_failure_falls_back_to_next_candidate(conn):
    good, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/1080", "Test Work 2020 1080p BluRay", size=1000)
    )
    fallback, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/720", "Test Work 2020 720p WEB-DL", size=500)
    )
    want_id = attach(conn, good)
    conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want_id, fallback.id))
    conn.commit()
    now = utcnow()

    assert taskgen.advance_want(conn, want_id, DL, now) == "NEW_TASK"
    task = statemachine.claim_task(conn, now)
    assert task.resource_id == good.id  # higher resolution ranks first

    outcome = taskgen.on_task_failed(
        conn, task.id, ErrorKind.PERMANENT, "dead link", POLICY, DL, now
    )
    assert outcome == "NEW_TASK"
    task2 = statemachine.claim_task(conn, now)
    assert task2.resource_id == fallback.id  # switched to the second candidate

    assert statemachine.complete_task(conn, task2.id, now) == "WANT_ACQUIRED"
    want = fetch_want(conn, want_id)
    assert want.status == WantStatus.ACQUIRED
    assert want.acquired_resource_id == fallback.id


def test_manual_retry_revives_given_up_want(conn):
    res, _ = ingest.ingest_resource(conn, make_resource("https://a.example/1", "Work 1080p"))
    want_id = attach(conn, res)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)
    taskgen.on_task_failed(conn, task.id, ErrorKind.PERMANENT, "404", POLICY, DL, now)
    assert fetch_want(conn, want_id).status == WantStatus.GIVEN_UP

    assert statemachine.retry_task(conn, task.id, utcnow())
    want = fetch_want(conn, want_id)
    assert want.status == WantStatus.WANTED
    assert fetch_task(conn, task.id).status.value == "QUEUED"
