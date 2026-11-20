"""Ingest pipeline: normalize -> fingerprint -> dedup -> parse -> match want.

Every path here is idempotent: running the same crawler output N times must
not create duplicate rows or duplicate tasks. Uniqueness is enforced by the
schema, not by caller discipline.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import matcher, parser
from .db import fetch_resource, resource_from_row
from .models import Resource
from .util import iso, utcnow

# Query parameters stripped before fingerprinting a URL.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "fbclid", "gclid", "ref", "ref_src", "mc_cid", "mc_eid",
})


@dataclass
class IngestResult:
    created: int = 0
    duplicates: int = 0


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((scheme, host, parts.path, urlencode(query), ""))


def compute_fingerprint(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def compute_content_fingerprint(normalized_title: str, size: int | None) -> str:
    """Identity of the *content* (title + size), independent of the site it
    was posted on. Empty when size is unknown (size is optional metadata)."""
    if not size:
        return ""
    return hashlib.sha256(f"{normalized_title}|{size}".encode("utf-8")).hexdigest()


def ingest_resource(conn: sqlite3.Connection, resource: Resource) -> tuple[Resource, bool]:
    """Insert one resource. Returns (resource, created). Never raises on duplicates."""
    normalized_title = parser.normalize_text(resource.title)
    work_key, quality = parser.parse_title(resource.title)
    fingerprint = compute_fingerprint(resource.source_url)
    size = resource.extra.get("size")
    content_fp = compute_content_fingerprint(normalized_title, size)
    now = iso(utcnow())

    # Same version re-posted elsewhere -> merge into the existing candidate.
    if content_fp:
        hit = conn.execute(
            "SELECT id FROM resources WHERE content_fingerprint=?", (content_fp,)
        ).fetchone()
        if hit:
            return fetch_resource(conn, hit["id"]), False

    try:
        cur = conn.execute(
            """
            INSERT INTO resources (
                fingerprint, content_fingerprint, title, normalized_title,
                source_url, source, resource_type, download_uris, published_at,
                extra, work_key, quality, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)
            ON CONFLICT(fingerprint) DO NOTHING
            """,
            (
                fingerprint,
                content_fp or None,
                resource.title,
                normalized_title,
                normalize_url(resource.source_url),
                resource.source,
                resource.resource_type.value,
                json.dumps(resource.download_uris, ensure_ascii=False),
                iso(resource.published_at),
                json.dumps(resource.extra, ensure_ascii=False),
                work_key,
                quality.to_json(),
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        # content_fingerprint partial unique index won a race; treat as duplicate.
        hit = conn.execute(
            "SELECT id FROM resources WHERE content_fingerprint=?", (content_fp,)
        ).fetchone()
        if hit:
            conn.rollback()
            return fetch_resource(conn, hit["id"]), False
        raise

    if cur.rowcount != 1:
        conn.commit()
        return fetch_resource_by_fingerprint(conn, fingerprint), False

    conn.commit()
    stored = fetch_resource(conn, cur.lastrowid)
    want_id = matcher.assign_want(conn, stored)
    if want_id:
        stored.want_id = want_id
    return stored, True


def fetch_resource_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Resource:
    row = conn.execute("SELECT * FROM resources WHERE fingerprint=?", (fingerprint,)).fetchone()
    return resource_from_row(row)


def ingest_resources(conn: sqlite3.Connection, resources: list[Resource]) -> IngestResult:
    result = IngestResult()
    for resource in resources:
        _, created = ingest_resource(conn, resource)
        if created:
            result.created += 1
        else:
            result.duplicates += 1
    return result
