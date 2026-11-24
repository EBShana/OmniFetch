"""Matcher: attach an ingested resource to a Want.

Two modes, both decided here in the core (crawlers stay unaware):
  1. subscription: the source config sets ``want_key`` in the resource's
     ``extra``; the resource belongs to that want directly.
  2. rule: each WANTED want carries ``match_rule``
     ``{"title_regex": "...", "aliases": [...]}``; the first matching rule wins.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from .db import fetch_want_by_key, want_from_row
from .models import Want, WantStatus
from .util import iso, utcnow

log = logging.getLogger(__name__)


def create_want(
    conn: sqlite3.Connection,
    work_key: str,
    display_title: str = "",
    match_rule: dict | None = None,
    rank_rule: dict | None = None,
) -> tuple[Want, int]:
    """Create a want if absent. Returns (want, matched_archived) where
    matched_archived is how many previously-ingested resources were
    attached via backfill (0 when the want already existed)."""
    validate_match_rule(match_rule or {})
    now = iso(utcnow())
    cur = conn.execute(
        """
        INSERT INTO wants (work_key, display_title, match_rule, rank_rule, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_key) DO NOTHING
        """,
        (work_key, display_title or work_key,
         json.dumps(match_rule or {}, ensure_ascii=False),
         json.dumps(rank_rule or {}, ensure_ascii=False),
         now, now),
    )
    conn.commit()
    want = fetch_want_by_key(conn, work_key)
    matched = 0
    if cur.rowcount == 1 and want.match_rule:
        # The want is new: attach resources ingested BEFORE it existed
        # (the crawl-archives-first, download-later workflow).
        matched = backfill_want(conn, want)
        if matched:
            log.info("backfill: want %s matched %d archived resources", work_key, matched)
    return want, matched


def find_or_create_want(
    conn: sqlite3.Connection,
    work_key: str,
    display_title: str = "",
    match_rule: dict | None = None,
    rank_rule: dict | None = None,
) -> Want:
    want, _ = create_want(conn, work_key, display_title, match_rule, rank_rule)
    return want


def backfill_want(conn: sqlite3.Connection, want: Want) -> int:
    """Attach already-ingested, unmatched resources to this want.
    Returns the number of resources attached."""
    count = 0
    stamp = iso(utcnow())
    rows = conn.execute(
        "SELECT id, normalized_title FROM resources WHERE want_id IS NULL"
    ).fetchall()
    for row in rows:
        if _rule_matches(want.match_rule, row["normalized_title"]):
            conn.execute(
                "UPDATE resources SET want_id=?, updated_at=? WHERE id=?",
                (want.id, stamp, row["id"]),
            )
            count += 1
    conn.commit()
    return count


def list_wants(conn: sqlite3.Connection, status: WantStatus | None = None) -> list[Want]:
    if status is not None:
        rows = conn.execute("SELECT * FROM wants WHERE status=? ORDER BY id", (status.value,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM wants ORDER BY id").fetchall()
    return [want_from_row(row) for row in rows]


def validate_match_rule(rule: dict) -> None:
    """Raise ValueError when title_regex is not a usable regular expression."""
    pattern = rule.get("title_regex")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"title_regex 无效：{exc}") from exc


def _rule_matches(rule: dict, normalized_title: str) -> bool:
    """title_regex is a regex; aliases are literal substrings, so titles
    containing '(' or '+' are matched as written instead of blowing up."""
    pattern = rule.get("title_regex")
    if pattern:
        try:
            if re.search(pattern, normalized_title, re.IGNORECASE):
                return True
        except re.error:
            log.warning("ignoring invalid title_regex %r", pattern)
    haystack = normalized_title.casefold()
    return any(str(alias).casefold() in haystack
               for alias in (rule.get("aliases") or []) if str(alias).strip())


def assign_want(conn: sqlite3.Connection, resource) -> int | None:
    """Attach a freshly ingested resource to a want; returns want_id or None."""
    want_id: int | None = None

    # 1) subscription mode
    want_key = (resource.extra or {}).get("want_key")
    if want_key:
        row = conn.execute(
            "SELECT id FROM wants WHERE work_key=?", (want_key,)
        ).fetchone()
        if row:
            want_id = row["id"]

    # 2) rule mode (only if subscription did not hit)
    if want_id is None:
        rows = conn.execute(
            "SELECT * FROM wants WHERE status=?", (WantStatus.WANTED.value,)
        ).fetchall()
        for row in rows:
            rule = json.loads(row["match_rule"] or "{}")
            if rule and _rule_matches(rule, resource.normalized_title):
                want_id = row["id"]
                break

    if want_id is not None:
        conn.execute(
            "UPDATE resources SET want_id=?, updated_at=? WHERE id=?",
            (want_id, iso(utcnow()), resource.id),
        )
        conn.commit()
    return want_id
