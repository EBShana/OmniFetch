"""Core mechanics: dedup, atomic claim, retry/backoff, fallback chain."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from omnifetch.config import Settings
from omnifetch.core import ingest, matcher, statemachine, taskgen
from omnifetch.core.db import connect, fetch_task, fetch_want, fetch_want_by_key, init_db
from omnifetch.core.matcher import find_or_create_want
from omnifetch.core.models import ErrorKind, Resource, ResourceType, WantStatus
from omnifetch.core.statemachine import FailOutcome, RetryPolicy
from omnifetch.core.util import iso, utcnow
from omnifetch.scheduler import runner, trigger

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


def test_aliases_are_literal_substrings_not_regex(conn):
    want = find_or_create_want(conn, "fri ren", "Frieren", {"aliases": ["fri.ren"]})
    assert want.id

    # "." is a literal dot now: it must not match "frieren".
    res, _ = ingest.ingest_resource(conn, make_resource("https://a.example/1", "Frieren 1080p"))
    assert matcher.assign_want(conn, res) is None

    # A literal "(" in an alias is also safe (an unescaped regex would raise).
    find_or_create_want(conn, "frieren 2023", "Frieren 2023",
                        {"aliases": ["frieren (2023)"]})
    res2, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/2", "Frieren (2023) 1080p"))
    assert matcher.assign_want(conn, res2) == fetch_want_by_key(conn, "frieren 2023").id


def test_invalid_title_regex_is_rejected_at_creation(conn):
    with pytest.raises(ValueError):
        find_or_create_want(conn, "bad regex", "Bad", {"title_regex": "(["})


def test_invalid_title_regex_cannot_crash_matching(conn):
    # A rule already stored with a broken pattern is skipped, not raised.
    assert matcher._rule_matches({"title_regex": "(["}, "anything") is False


def test_reopen_restores_redundant_candidates(conn):
    first, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/1080", "Work 2020 1080p BluRay", size=1000)
    )
    second, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/720", "Work 2020 720p WEB-DL", size=500)
    )
    want_id = attach(conn, first)
    conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want_id, second.id))
    conn.commit()
    now = utcnow()

    assert taskgen.advance_want(conn, want_id, DL, now) == "NEW_TASK"
    task = statemachine.claim_task(conn, now)
    assert statemachine.complete_task(conn, task.id, now) == "WANT_ACQUIRED"

    def statuses() -> dict[int, str]:
        return {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM resources WHERE want_id=?", (want_id,))}

    assert statuses()[second.id] == "REDUNDANT"

    statemachine.revive_want(conn, want_id, now)
    assert fetch_want(conn, want_id).status == WantStatus.WANTED
    after = statuses()
    assert after[first.id] == "TASKED"   # already downloaded, not retried
    assert after[second.id] == "NEW"     # fallback is a candidate again

    assert taskgen.advance_want(conn, want_id, DL, utcnow()) == "NEW_TASK"
    queued = conn.execute("SELECT resource_id FROM tasks WHERE status='QUEUED'").fetchone()
    assert queued["resource_id"] == second.id


def test_no_downloader_falls_through_to_next_candidate(conn):
    high, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/1080", "Work 2020 1080p BluRay", size=1000)
    )
    low, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/720", "Work 2020 720p WEB-DL", size=500)
    )
    want_id = attach(conn, high)
    conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want_id, low.id))
    conn.commit()

    def only_low(resource: Resource) -> str | None:
        return None if resource.id == high.id else "test-dl"

    assert taskgen.advance_want(conn, want_id, only_low, utcnow()) == "NEW_TASK"
    task = conn.execute("SELECT resource_id FROM tasks").fetchone()
    assert task["resource_id"] == low.id
    assert fetch_want(conn, want_id).status == WantStatus.WANTED


def test_no_downloader_for_any_candidate_gives_up(conn):
    res, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/1", "Work 1080p")
    )
    want_id = attach(conn, res)

    assert taskgen.advance_want(conn, want_id, lambda r: None, utcnow()) == "NO_DOWNLOADER"
    assert fetch_want(conn, want_id).status == WantStatus.GIVEN_UP


def test_preference_lists_rank_by_position():
    rule = {"prefer_sources": ["vcb-nyaa", "vcb-dmhy", "other"],
            "prefer_groups": ["GroupA", "GroupB"]}

    def made(source: str, group: str | None) -> Resource:
        res = Resource(title="x", source_url="https://x/" + source, source=source)
        res.quality.group = group
        res.quality.resolution = 1080
        return res

    first, second, third, unlisted = (
        taskgen.score(made(name, None), rule) for name in
        ("vcb-nyaa", "vcb-dmhy", "other", "unlisted")
    )
    assert first[0] > second[0] > third[0] > unlisted[0] == 0

    group_a = taskgen.score(made("unlisted", "GroupA"), rule)
    group_b = taskgen.score(made("unlisted", "GroupB"), rule)
    no_group = taskgen.score(made("unlisted", None), rule)
    assert group_a[1] > group_b[1] > no_group[1] == 0


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


# ------------------------------------------------- VCB auto-want per row

def vcb_resource(url: str, title: str, uid: str) -> Resource:
    return Resource(
        title=title,
        source_url=url,
        source="vcb-dmhy",
        download_uris=[f"magnet:?xt=urn:btih:{uid}"],
        resource_type=ResourceType.MAGNET,
        extra={"vcb_uid": uid, "want_key": f"vcb-{uid}", "want_title": title},
    )


def want_id_of(conn, resource: Resource) -> int | None:
    return conn.execute(
        "SELECT want_id FROM resources WHERE id=?", (resource.id,)
    ).fetchone()["want_id"]


def test_new_sheet_row_creates_one_want_for_all_mirrors(conn):
    title = "Guild no Uketsukejou Desu ga\n虽然是公会的柜台小姐"
    dmhy = vcb_resource("https://share.dmhy.org/topics/view/1_x.html", title, "261102")
    stored, created = ingest.ingest_resource(conn, dmhy)
    assert created
    assert ingest.ensure_wants(conn, [dmhy]) == 1

    want = fetch_want_by_key(conn, "vcb-261102")
    assert want is not None
    assert want.display_title == title
    assert want.status == WantStatus.WANTED
    assert want_id_of(conn, stored) == want.id

    nyaa = Resource(
        title=title, source_url="https://nyaa.si/view/2184807", source="vcb-nyaa",
        download_uris=["https://nyaa.si/download/2184807.torrent"],
        resource_type=ResourceType.TORRENT,
        extra={"vcb_uid": "261102", "want_key": "vcb-261102", "want_title": title},
    )
    mirror, _ = ingest.ingest_resource(conn, nyaa)
    assert want_id_of(conn, mirror) == want.id
    assert ingest.ensure_wants(conn, [nyaa]) == 0
    assert conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0] == 1

    # The normal dispatch chain picks the row up without any manual step.
    assert taskgen.advance_want(conn, want.id, DL, utcnow()) == "NEW_TASK"


def test_recrawled_sheet_row_does_not_create_a_second_want(conn):
    url = "https://share.dmhy.org/topics/view/2_x.html"
    title = "Mayonaka Heart Tune\n午夜的倾心旋律"
    first = vcb_resource(url, title, "261101")
    _, created = ingest.ingest_resource(conn, first)
    assert created
    ingest.ensure_wants(conn, [first])

    again_res = vcb_resource(url, title, "261101")
    again, created_again = ingest.ingest_resource(conn, again_res)
    assert not created_again
    assert ingest.ensure_wants(conn, [again_res]) == 0
    assert conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 1
    assert again.want_id == fetch_want_by_key(conn, "vcb-261101").id


def test_collapsed_duplicate_row_does_not_add_another_want(conn):
    # Same release posted twice on the sheet (title+size identical): the
    # second row merges into the existing candidate and must not create a
    # second want, otherwise the same release would be downloaded twice.
    first = vcb_resource("https://share.dmhy.org/topics/view/3_a.html",
                         "Same Release 1080p", "300001")
    second = vcb_resource("https://share.dmhy.org/topics/view/3_b.html",
                          "Same Release 1080p", "300002")
    first.extra["size"] = second.extra["size"] = 1000

    _, created1 = ingest.ingest_resource(conn, first)
    assert created1
    ingest.ensure_wants(conn, [first])
    _, created2 = ingest.ingest_resource(conn, second)
    assert not created2
    assert ingest.ensure_wants(conn, [second]) == 0
    assert conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0] == 1
    assert fetch_want_by_key(conn, "vcb-300002") is None


def test_row_ingested_before_feature_gets_its_want_on_next_crawl(conn):
    # The current DB already holds VCB rows ingested before auto-want existed.
    # Their next crawl is a duplicate resource, but the want must still appear.
    url = "https://share.dmhy.org/topics/view/4_x.html"
    title = "Old Row\n旧条目"
    stored, created = ingest.ingest_resource(conn, vcb_resource(url, title, "400001"))
    assert created
    assert conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0] == 0

    fresh = vcb_resource(url, title, "400001")
    _, created_again = ingest.ingest_resource(conn, fresh)
    assert not created_again
    assert ingest.ensure_wants(conn, [fresh]) == 1

    want = fetch_want_by_key(conn, "vcb-400001")
    assert want_id_of(conn, stored) == want.id
    assert taskgen.advance_want(conn, want.id, DL, utcnow()) == "NEW_TASK"


# ------------------------------------------------------- crawl scheduling

class _RecordingCrawler:
    def __init__(self, interval: int, error: Exception | None = None):
        self.config = {"interval": interval}
        self.error = error
        self.calls = 0
        self._stop = threading.Event()

    def reset_stop(self):
        self._stop.clear()

    def request_stop(self):
        self._stop.set()

    def should_stop(self):
        return self._stop.is_set()

    def fetch(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return []

    def fetch_batches(self):
        from omnifetch.crawlers.base import CrawlBatch
        yield CrawlBatch(list(self.fetch()))


def _crawl_ctx(tmp_path, crawler):
    settings = Settings(db_path=str(tmp_path / "crawl.db"),
                        config_dir=str(tmp_path / "cfg"))
    return runner.Context(settings, {"demo": crawler}, {}, [])


def test_crawler_failure_keeps_detail_and_retries_before_long_interval(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    crawler = _RecordingCrawler(interval=30 * 24 * 3600,
                                error=RuntimeError("sheet returned 503"))
    ctx = _crawl_ctx(tmp_path, crawler)
    now = utcnow()

    trigger.run_due_crawlers(conn, ctx, now)
    ctx.wait_crawlers(5)
    assert crawler.calls == 1
    row = conn.execute("SELECT error FROM source_runs").fetchone()
    assert "RuntimeError" in row["error"] and "sheet returned 503" in row["error"]

    # No hammering right after a failure ...
    trigger.run_due_crawlers(conn, ctx, now + timedelta(seconds=60))
    ctx.wait_crawlers(5)
    assert crawler.calls == 1

    # ... but a monthly source is retried within minutes, not next month.
    trigger.run_due_crawlers(conn, ctx, now + timedelta(seconds=301))
    ctx.wait_crawlers(5)
    assert crawler.calls == 2
    conn.close()


def test_successful_crawler_keeps_its_full_interval(tmp_path):
    conn = connect(str(tmp_path / "crawl.db"))
    init_db(conn)
    crawler = _RecordingCrawler(interval=3600)
    ctx = _crawl_ctx(tmp_path, crawler)
    now = utcnow()

    trigger.run_due_crawlers(conn, ctx, now)
    ctx.wait_crawlers(5)
    trigger.run_due_crawlers(conn, ctx, now + timedelta(seconds=600))
    ctx.wait_crawlers(5)
    assert crawler.calls == 1

    trigger.run_due_crawlers(conn, ctx, now + timedelta(seconds=3601))
    ctx.wait_crawlers(5)
    assert crawler.calls == 2
    conn.close()


def test_run_forever_ticks_until_stopped(monkeypatch, tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"),
                        config_dir=str(tmp_path / "cfg"), poll_interval=0)
    ctx = runner.Context(settings, {}, {}, [])
    stop = threading.Event()
    ticks = []

    def fake_tick(_ctx):
        ticks.append(1)
        if len(ticks) == 3:
            stop.set()

    monkeypatch.setattr(runner, "run_once", fake_tick)
    runner.run_forever(ctx, stop)
    assert len(ticks) == 3


def test_want_without_resources_waits_instead_of_giving_up(conn):
    want = find_or_create_want(conn, "not-discovered-yet")
    assert want.status == WantStatus.WANTED

    assert taskgen.advance_want(conn, want.id, DL, utcnow()) == "WAITING"
    assert fetch_want(conn, want.id).status == WantStatus.WANTED

    res, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/late", "Work 1080p")
    )
    conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want.id, res.id))
    conn.commit()

    assert taskgen.advance_want(conn, want.id, DL, utcnow()) == "NEW_TASK"


def test_fail_task_on_non_downloading_task_is_noop(conn):
    res, _ = ingest.ingest_resource(
        conn, make_resource("https://a.example/noop", "Work 1080p")
    )
    want_id = attach(conn, res)
    now = utcnow()
    taskgen.advance_want(conn, want_id, DL, now)
    task = statemachine.claim_task(conn, now)

    first = statemachine.fail_task(
        conn, task.id, ErrorKind.PERMANENT, "404", POLICY, now
    )
    second = statemachine.fail_task(
        conn, task.id, ErrorKind.PERMANENT, "404", POLICY, now
    )
    assert first == FailOutcome.FAILED
    assert second == FailOutcome.NOOP
