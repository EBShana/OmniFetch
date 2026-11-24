"""End-to-end check against the real aria2 container: build a want, ingest a
resource, then run the scheduler pieces (generate_all -> dispatch -> poll)
until the task completes, and verify the file landed in ./downloads."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from omnifetch.config import Settings
from omnifetch.core import ingest, taskgen
from omnifetch.core.matcher import find_or_create_want
from omnifetch.core.models import Resource, ResourceType
from omnifetch.core.util import utcnow
from omnifetch.scheduler.runner import build_context, poller

TEST_URL = sys.argv[1] if len(sys.argv) > 1 else "https://proof.ovh.net/files/1Mb.dat"
DB = "./data/it.db"

Path(DB).unlink(missing_ok=True)
for old in Path("./downloads").glob("1Mb.dat*"):
    old.unlink()

settings = Settings(db_path=DB, config_dir="./config-it")
ctx = build_context(settings)
assert ctx.downloaders, "aria2 downloader not loaded from config-it"

conn = ctx.conn()
want = find_or_create_want(conn, "integration-test")
res, created = ingest.ingest_resource(
    conn,
    Resource(
        title="1Mb.dat", source_url=TEST_URL, source="itest",
        download_uris=[TEST_URL], resource_type=ResourceType.HTTP,
    ),
)
conn.execute("UPDATE resources SET want_id=? WHERE id=?", (want.id, res.id))
conn.commit()
print(f"resource id={res.id} created={created}")

deadline = time.time() + 240
final_status = None
while time.time() < deadline:
    now = utcnow()
    taskgen.generate_all(conn, ctx.downloader_for, now)
    poller.dispatch(conn, ctx, now)
    poller.poll_active(conn, ctx, now)
    row = conn.execute(
        "SELECT id, status, external_id, error_message FROM tasks WHERE resource_id=?",
        (res.id,),
    ).fetchone()
    if row:
        final_status = dict(row)
        print(f"  task {row['id']}: {row['status']} ext={row['external_id']} {row['error_message'] or ''}")
        if row["status"] in ("COMPLETED", "FAILED"):
            break
    time.sleep(4)

assert final_status and final_status["status"] == "COMPLETED", f"did not complete: {final_status}"
files = list(Path("./downloads").rglob("1Mb.dat*"))
assert files, "downloaded file missing in ./downloads"
print(f"SUCCESS: downloaded {files[0]} ({files[0].stat().st_size} bytes)")
