"""Web UI + API integration tests: pages, fragments, auth, form actions."""
from __future__ import annotations

import inspect
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from omnifetch.app import create_app
from omnifetch.config import Settings
from omnifetch.core import db as core_db
from omnifetch.core import ingest
from omnifetch.core.matcher import find_or_create_want
from omnifetch.core.models import Resource, ResourceType
from omnifetch.core.util import iso, utcnow


@pytest.fixture
def env(tmp_path):
    (tmp_path / "cfg").mkdir()
    settings = Settings(db_path=str(tmp_path / "t.db"), config_dir=str(tmp_path / "cfg"))
    return create_app(settings), settings


@pytest.fixture
def client(env):
    return TestClient(env[0])


def _seed_want(settings) -> int:
    conn = core_db.connect(settings.db_path)
    want = find_or_create_want(conn, "demo work", "演示作品",
                               {"aliases": ["demo"]}, {"prefer_sources": ["vcb-nyaa"]})
    res, _ = ingest.ingest_resource(
        conn,
        Resource(title="Demo Work 1080p", source_url="https://x.example/1",
                 source="vcb-nyaa",
                 download_uris=["https://x.example/1.torrent"],
                 resource_type=ResourceType.TORRENT),
    )
    conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want.id, res.id))
    conn.commit()
    conn.close()
    return want.id


def test_web_requests_use_a_connection_from_their_own_thread(env, monkeypatch):
    """Regression: /frag/failures returned 500 under load with
    "SQLite objects created in a thread can only be used in that same thread".

    FastAPI runs sync ``yield`` dependencies and sync endpoints in a
    threadpool, so a connection handed over by a dependency may cross
    threads. Endpoints must therefore open/close their own connection.
    """
    app, _ = env

    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            assert "conn" not in inspect.signature(endpoint).parameters, route.path

    violations: list[str] = []
    closes: list[tuple[int, int]] = []

    class _ThreadBoundConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.origin = threading.get_ident()

        def execute(self, *args, **kwargs):
            if threading.get_ident() != self.origin:
                violations.append(args[0])
            return super().execute(*args, **kwargs)

        def close(self):
            closes.append((self.origin, threading.get_ident()))
            super().close()

    def spy_connect(db_path):
        conn = sqlite3.connect(db_path, timeout=30, factory=_ThreadBoundConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(core_db, "connect", spy_connect)
    client = TestClient(app)
    paths = ["/frag/failures", "/frag/tasks", "/frag/wants", "/frag/dashboard",
             "/api/tasks", "/api/wants", "/api/stats"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda i: client.get(paths[i % len(paths)]), range(28)))

    assert [r.status_code for r in responses] == [200] * len(responses)
    assert violations == []
    assert closes and all(created == used for created, used in closes)


def test_dashboard_shows_running_crawl_progress(env):
    _, settings = env
    conn = core_db.connect(settings.db_path)
    conn.execute(
        "INSERT INTO source_runs (source, started_at, done, total) VALUES ('vcb', ?, 3, 10)",
        (iso(utcnow()),),
    )
    conn.commit()
    conn.close()

    html = TestClient(env[0]).get("/frag/dashboard").text
    assert "抓取中" in html
    assert "3/10" in html


def test_pages_and_static_render(client):
    for path in ["/", "/wants", "/tasks", "/failures", "/status"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "OmniFetch" in response.text
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_health_open_and_reports_auth(client):
    assert client.get("/api/health").json() == {"ok": True, "auth_required": False}


def test_fragments_show_seeded_want(client, env):
    _, settings = env
    want_id = _seed_want(settings)

    html = client.get("/frag/wants").text
    assert "演示作品" in html and "WANTED" in html

    html = client.get(f"/frag/wants/{want_id}/candidates").text
    assert "vcb-nyaa" in html and "候选链" in html

    data = client.get(f"/api/wants/{want_id}/candidates").json()
    assert data["want"]["work_key"] == "demo work"
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["source"] == "vcb-nyaa"


def test_stats_endpoint(client, env):
    _, settings = env
    _seed_want(settings)
    stats = client.get("/api/stats").json()
    assert stats["wants"]["WANTED"] == 1
    assert stats["tasks"]["COMPLETED"] == 0


def test_ui_form_actions_and_toast(client, env):
    response = client.post("/ui/wants", data={
        "work_key": "second work",
        "aliases": "别名a, 别名b",
        "prefer_sources": "vcb-nyaa, vcb-dmhy",
    })
    assert response.status_code == 200
    assert "second work" in response.text          # refreshed wants fragment
    assert "toast" in response.headers["hx-trigger"]

    works = client.get("/api/wants").json()
    assert any(w["work_key"] == "second work" for w in works)


def test_task_retry_and_ignore_via_api(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    conn = core_db.connect(settings.db_path)
    row = conn.execute("SELECT id FROM resources WHERE want_id=?", (want_id,)).fetchone()
    now = iso(utcnow())
    conn.execute(
        "INSERT INTO tasks (resource_id, downloader, status, created_at, updated_at)"
        " VALUES (?, 'test-dl', 'FAILED', ?, ?)",
        (row["id"], now, now),
    )
    conn.commit()
    task_id = conn.execute("SELECT id FROM tasks").fetchone()["id"]
    conn.close()

    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 200
    tasks = client.get("/api/tasks").json()
    assert tasks[0]["status"] == "QUEUED"

    response = client.post(f"/ui/tasks/{task_id}/ignore")
    assert response.status_code == 200
    assert "toast" in response.headers["hx-trigger"]
    assert client.get("/api/tasks").json()[0]["status"] == "IGNORED"


def _seed_task(settings, want_id: int, status: str) -> int:
    conn = core_db.connect(settings.db_path)
    resource_id = conn.execute(
        "SELECT id FROM resources WHERE want_id=?", (want_id,)
    ).fetchone()["id"]
    now = iso(utcnow())
    cur = conn.execute(
        "INSERT INTO tasks (resource_id, downloader, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (resource_id, f"test-dl-{status.lower()}", status, now, now),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id


def test_failure_fragment_actions_stay_on_failures_view(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    task_id = _seed_task(settings, want_id, "FAILED")

    html = client.post(f"/ui/tasks/{task_id}/ignore", data={"view": "failures"}).text
    assert "失败的任务" in html and "放弃的作品" in html
    assert "<th>下载器</th>" not in html      # not the tasks table
    assert f'<td class="muted">{task_id}</td>' not in html  # left the queue


def test_given_up_fragment_actions_stay_on_failures_view(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    conn = core_db.connect(settings.db_path)
    conn.execute("UPDATE wants SET status='GIVEN_UP' WHERE id=?", (want_id,))
    conn.commit()
    conn.close()

    html = client.post(f"/ui/wants/{want_id}/reopen", data={"view": "failures"}).text
    assert "失败的任务" in html and "放弃的作品" in html


def test_task_action_keeps_status_filter(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    failed_id = _seed_task(settings, want_id, "FAILED")
    queued_id = _seed_task(settings, want_id, "QUEUED")

    html = client.post(f"/ui/tasks/{failed_id}/ignore",
                       data={"view": "tasks", "status": "FAILED"}).text
    assert f'<td class="muted">{queued_id}</td>' not in html
    assert "没有匹配的任务" in html


def test_fragments_carry_the_active_view(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    _seed_task(settings, want_id, "FAILED")
    _seed_task(settings, want_id, "QUEUED")

    assert '"view": "failures"' in client.get("/frag/failures").text
    tasks_html = client.get("/frag/tasks", params={"status": "FAILED"}).text
    assert '"view": "tasks", "status": "FAILED"' in tasks_html


def test_invalid_title_regex_returns_400(client):
    response = client.post("/api/wants", json={
        "work_key": "bad regex",
        "match_rule": {"title_regex": "(["},
    })
    assert response.status_code == 400
    assert client.get("/api/wants").json() == []


def test_get_candidates_query_count_does_not_grow_with_rows(env):
    from omnifetch.webapi.app import get_candidates

    _, settings = env
    want_id = _seed_want(settings)
    conn = core_db.connect(settings.db_path)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = get_candidates(conn, want_id)
    assert len(data["candidates"]) == 1
    baseline = len(statements)

    for i in range(4):
        res, _ = ingest.ingest_resource(
            conn,
            Resource(title=f"Extra {i} 1080p", source_url=f"https://x.example/{i}",
                     source="vcb-nyaa", download_uris=[f"https://x.example/{i}"],
                     resource_type=ResourceType.HTTP),
        )
        conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want_id, res.id))
    conn.commit()

    expected = conn.execute(
        "SELECT COUNT(*) FROM resources WHERE want_id=?", (want_id,)).fetchone()[0]
    statements.clear()
    data = get_candidates(conn, want_id)
    conn.set_trace_callback(None)
    conn.close()
    assert len(data["candidates"]) == expected > 1
    assert len(statements) == baseline, statements


def test_retry_next_ignores_downloading_task(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    conn = core_db.connect(settings.db_path)
    resource_id = conn.execute(
        "SELECT id FROM resources WHERE want_id=?", (want_id,)
    ).fetchone()["id"]
    now = iso(utcnow())
    conn.execute(
        "INSERT INTO tasks (resource_id, downloader, status, created_at, updated_at)"
        " VALUES (?, 'test-dl', 'DOWNLOADING', ?, ?)",
        (resource_id, now, now),
    )
    conn.commit()
    task_id = conn.execute("SELECT id FROM tasks").fetchone()["id"]
    conn.close()

    assert client.post(f"/api/wants/{want_id}/retry-next").status_code == 200
    tasks = {t["id"]: t for t in client.get("/api/tasks").json()}
    assert tasks[task_id]["status"] == "IGNORED"


def test_ui_retry_next_stops_a_downloading_task(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    _seed_task(settings, want_id, "DOWNLOADING")

    response = client.post(f"/ui/wants/{want_id}/retry-next")
    assert response.status_code == 200
    assert client.get("/api/tasks").json()[0]["status"] == "IGNORED"


def test_reopen_endpoint(client, env):
    _, settings = env
    want_id = _seed_want(settings)
    conn = core_db.connect(settings.db_path)
    conn.execute("UPDATE wants SET status='GIVEN_UP' WHERE id=?", (want_id,))
    conn.commit()
    conn.close()

    assert client.post(f"/api/wants/{want_id}/reopen").status_code == 200
    assert client.get("/api/wants").json()[0]["status"] == "WANTED"


def test_token_auth(tmp_path):
    (tmp_path / "cfg").mkdir()
    settings = Settings(db_path=str(tmp_path / "t.db"), config_dir=str(tmp_path / "cfg"),
                        token="s3cret")
    client = TestClient(create_app(settings))

    assert client.get("/api/health").json()["auth_required"] is True
    assert client.get("/api/wants").status_code == 401
    assert client.get("/frag/wants").status_code == 401
    assert client.post("/ui/wants", data={"work_key": "x"}).status_code == 401

    headers = {"X-OmniFetch-Token": "s3cret"}
    assert client.get("/api/auth/verify").status_code == 401
    assert client.get("/api/auth/verify",
                      headers={"X-OmniFetch-Token": "wrong"}).status_code == 401
    assert client.get("/api/auth/verify", headers=headers).json() == {"ok": True}
    assert client.get("/api/wants", headers=headers).status_code == 200
    assert client.get("/frag/wants", headers=headers).status_code == 200
    assert client.get("/api/wants", params={"token": "s3cret"}).status_code == 200
    assert client.get("/api/wants",
                      headers={"Authorization": "Bearer s3cret"}).status_code == 200

    client.cookies.set("omnifetch_token", "s3cret")
    assert client.get("/frag/wants").status_code == 200

    assert client.get("/").status_code == 200  # shell pages stay open


def test_app_js_verifies_token_and_clears_cookie(client):
    js = client.get("/static/app.js").text
    assert "/api/auth/verify" in js
    assert "omnifetch_token=; path=/; max-age=0" in js
