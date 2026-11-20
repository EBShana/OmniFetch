"""Scheduler context and main loop.

One run_once() tick = reap zombies -> run due crawlers -> advance fallback
chains -> dispatch queued tasks -> poll active downloads -> notify.
Everything is idempotent; the loop may crash and restart at any point.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import Settings, enabled, load_plugin_configs
from ..core import db, taskgen
from ..core.models import Resource
from ..core.util import utcnow
from ..crawlers import create_crawler
from ..downloaders import create_downloader
from ..notify import Event, Notifier, NullNotifier, WebhookNotifier
from . import poller, reaper, trigger

log = logging.getLogger(__name__)


@dataclass
class Context:
    settings: Settings
    crawlers: dict  # name -> CrawlerPlugin
    downloaders: dict  # name -> DownloaderPlugin
    notifiers: list[Notifier]
    last_runs: dict[str, datetime] = field(default_factory=dict)

    def conn(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path)

    def downloader_for(self, resource: Resource) -> str | None:
        for name, downloader in self.downloaders.items():
            if downloader.supports(resource):
                return name
        return None

    def notify(self, event_type: str, payload: dict) -> None:
        event = Event(type=event_type, payload=payload)
        for notifier in self.notifiers:
            try:
                notifier.send(event)
            except Exception:
                log.exception("notifier failed for event %s", event_type)


def build_context(settings: Settings) -> Context:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()

    sources_cfg, downloaders_cfg = load_plugin_configs(settings.config_dir)
    crawlers, downloaders = {}, {}
    for name, cfg in enabled(sources_cfg).items():
        try:
            crawlers[name] = create_crawler(cfg["type"], name, cfg)
        except Exception:
            log.exception("failed to load crawler %s (%s)", name, cfg.get("type"))
    for name, cfg in enabled(downloaders_cfg).items():
        try:
            downloaders[name] = create_downloader(cfg["type"], name, cfg)
        except Exception:
            log.exception("failed to load downloader %s (%s)", name, cfg.get("type"))

    notifiers: list[Notifier] = [NullNotifier()]
    if settings.webhook_url:
        notifiers.append(WebhookNotifier(settings.webhook_url))

    log.info(
        "context ready: %d crawlers, %d downloaders",
        len(crawlers), len(downloaders),
    )
    return Context(settings, crawlers, downloaders, notifiers)


def run_once(ctx: Context) -> None:
    now = utcnow()
    conn = ctx.conn()
    try:
        reaped = reaper.sweep(conn, ctx, now)
        if reaped:
            log.info("reaper: requeued %d zombie tasks", reaped)
        trigger.run_due_crawlers(conn, ctx, now)
        for want_id, outcome in taskgen.generate_all(conn, ctx.downloader_for, now):
            if outcome == "GIVE_UP":
                ctx.notify("want.given_up", {"want_id": want_id})
            elif outcome == "NO_DOWNLOADER":
                ctx.notify("want.given_up", {"want_id": want_id, "reason": "no_downloader"})
        poller.dispatch(conn, ctx, now)
        poller.poll_active(conn, ctx, now)
    finally:
        conn.close()
