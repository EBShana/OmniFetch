"""SQLite storage: the single persistent layer of the system.

Unique constraints are the hard guarantee against duplicates:
  - wants.work_key              -> one want per work
  - resources.fingerprint       -> one row per normalized source URL
  - resources.content_fingerprint (partial unique index)
                                -> the same version re-posted on other sites
                                   merges into ONE candidate
  - tasks (resource_id, downloader) -> one task per candidate/downloader pair
"""
from __future__ import annotations

import json
import sqlite3

from .models import (
    ErrorKind,
    Quality,
    Resource,
    ResourceType,
    Task,
    TaskStatus,
    Want,
    WantStatus,
)
from .util import parse_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    content_fingerprint TEXT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    source TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    download_uris TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    extra TEXT NOT NULL DEFAULT '{}',
    work_key TEXT NOT NULL DEFAULT '',
    want_id INTEGER,
    quality TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'NEW',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_resources_content_fp
    ON resources(content_fingerprint)
    WHERE content_fingerprint IS NOT NULL AND content_fingerprint != '';
CREATE INDEX IF NOT EXISTS ix_resources_want ON resources(want_id);

CREATE TABLE IF NOT EXISTS wants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_key TEXT NOT NULL UNIQUE,
    display_title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'WANTED'
        CHECK (status IN ('WANTED', 'ACQUIRED', 'GIVEN_UP')),
    match_rule TEXT NOT NULL DEFAULT '{}',
    rank_rule TEXT NOT NULL DEFAULT '{}',
    acquired_resource_id INTEGER REFERENCES resources(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    downloader TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN
        ('QUEUED', 'DOWNLOADING', 'FAILED_RETRYABLE', 'FAILED', 'COMPLETED', 'IGNORED')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    claimed_at TEXT,
    external_id TEXT,
    progress REAL NOT NULL DEFAULT 0,
    error_kind TEXT CHECK (error_kind IN ('transient', 'permanent')),
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (resource_id, downloader)
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status, next_retry_at);
CREATE INDEX IF NOT EXISTS ix_tasks_resource ON tasks(resource_id);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_runs ON source_runs(source, id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- row mappers

def resource_from_row(row: sqlite3.Row) -> Resource:
    return Resource(
        id=row["id"],
        fingerprint=row["fingerprint"] or "",
        content_fingerprint=row["content_fingerprint"] or "",
        title=row["title"],
        normalized_title=row["normalized_title"],
        source_url=row["source_url"],
        source=row["source"],
        resource_type=ResourceType(row["resource_type"]),
        download_uris=json.loads(row["download_uris"] or "[]"),
        published_at=parse_iso(row["published_at"]),
        extra=json.loads(row["extra"] or "{}"),
        work_key=row["work_key"],
        want_id=row["want_id"],
        quality=Quality.from_json(row["quality"]),
        status=row["status"],
    )


def want_from_row(row: sqlite3.Row) -> Want:
    return Want(
        id=row["id"],
        work_key=row["work_key"],
        display_title=row["display_title"],
        status=WantStatus(row["status"]),
        match_rule=json.loads(row["match_rule"] or "{}"),
        rank_rule=json.loads(row["rank_rule"] or "{}"),
        acquired_resource_id=row["acquired_resource_id"],
    )


def task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        resource_id=row["resource_id"],
        downloader=row["downloader"],
        status=TaskStatus(row["status"]),
        retry_count=row["retry_count"],
        next_retry_at=parse_iso(row["next_retry_at"]),
        claimed_at=parse_iso(row["claimed_at"]),
        external_id=row["external_id"],
        progress=row["progress"],
        error_kind=ErrorKind(row["error_kind"]) if row["error_kind"] else None,
        error_message=row["error_message"],
    )


# ------------------------------------------------------------------- fetchers

def fetch_resource(conn: sqlite3.Connection, resource_id: int) -> Resource | None:
    row = conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
    return resource_from_row(row) if row else None


def fetch_resource_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Resource | None:
    row = conn.execute("SELECT * FROM resources WHERE fingerprint=?", (fingerprint,)).fetchone()
    return resource_from_row(row) if row else None


def fetch_want(conn: sqlite3.Connection, want_id: int) -> Want | None:
    row = conn.execute("SELECT * FROM wants WHERE id=?", (want_id,)).fetchone()
    return want_from_row(row) if row else None


def fetch_want_by_key(conn: sqlite3.Connection, work_key: str) -> Want | None:
    row = conn.execute("SELECT * FROM wants WHERE work_key=?", (work_key,)).fetchone()
    return want_from_row(row) if row else None


def fetch_task(conn: sqlite3.Connection, task_id: int) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return task_from_row(row) if row else None
