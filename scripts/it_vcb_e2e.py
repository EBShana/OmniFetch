"""Full-chain integration test: VCB crawl -> dedup -> want matching ->
fallback chain -> aria2 download (progress confirmed, then cancelled).

Uses config-it/ (real vcb cookie + aria2 against localhost:6800).
"""
from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from omnifetch.config import Settings
from omnifetch.core import taskgen
from omnifetch.core.matcher import find_or_create_want
from omnifetch.core.statemachine import RetryPolicy
from omnifetch.core.util import utcnow
from omnifetch.scheduler import trigger
from omnifetch.scheduler.runner import build_context, poller

DB = "./data/it-vcb.db"
Path(DB).unlink(missing_ok=True)

settings = Settings(db_path=DB, config_dir="./config-it")
# test-only acceleration: stall after 1 minute, quick single retry
settings.stalled_after = timedelta(minutes=1)
settings.retry = RetryPolicy(max_retries=1, base_seconds=20, max_seconds=60)
ctx = build_context(settings)
assert ctx.crawlers.get("vcb"), "vcb crawler not loaded"
assert ctx.downloaders.get("aria2"), "aria2 downloader not loaded"

conn = ctx.conn()
# Want created BEFORE the crawl: rule-mode matching attaches at ingest time.
# prefer_sources: nyaa torrent (healthy swarm) before dmhy magnet.
want = find_or_create_want(
    conn,
    "tsue to tsurugi no wistoria",
    "Tsue to Tsurugi no Wistoria / 杖与剑的魔剑谭",
    match_rule={"aliases": ["wistoria", "杖与剑"]},
    rank_rule={"prefer_sources": ["vcb-nyaa"]},
)

print("tick 0: crawling (first run fetches several VCB pages) ...")
deadline = time.time() + 420
cancelled = False
while time.time() < deadline:
    now = utcnow()
    trigger.run_due_crawlers(conn, ctx, now)
    for want_id, outcome in taskgen.generate_all(conn, ctx.downloader_for, now):
        print(f"  generate_all: want {want_id} -> {outcome}")
    poller.dispatch(conn, ctx, now)
    poller.poll_active(conn, ctx, now)

    rows = conn.execute(
        """
        SELECT t.id, t.status, t.progress, t.external_id, r.source, r.title
        FROM tasks t JOIN resources r ON r.id = t.resource_id
        WHERE r.want_id = ? ORDER BY t.id
        """,
        (want.id,),
    ).fetchall()
    for row in rows:
        title = (row["title"] or "").splitlines()[0][:38]
        print(f"  task {row['id']} [{row['source']}] {row['status']:12s} "
              f"progress={row['progress']:.3f} {title}")
    active = [r for r in rows if r["status"] == "DOWNLOADING"]
    if active and active[0]["progress"] > 0:
        print(f"SUCCESS: download running in aria2 (gid {active[0]['external_id']})")
        ctx.downloaders["aria2"].cancel(active[0]["external_id"])
        cancelled = True
        break
    if any(r["status"] == "COMPLETED" for r in rows):
        print("SUCCESS: already completed")
        break
    time.sleep(5)

stats = conn.execute(
    "SELECT source, COUNT(*) c FROM resources GROUP BY source"
).fetchall()
print("ingested:", {r["source"]: r["c"] for r in stats})
want_row = conn.execute("SELECT status FROM wants WHERE id=?", (want.id,)).fetchone()
print("want status:", want_row["status"], "| demo download cancelled:", cancelled)
assert stats, "nothing ingested"
assert cancelled, "download never started in aria2"
