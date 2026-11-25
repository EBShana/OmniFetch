"""Scheduler context and main loop.

One run_once() tick = reap zombies -> run due crawlers -> advance fallback
chains -> dispatch queued tasks -> poll active downloads -> notify.
Everything is idempotent; the loop may crash and restart at any point.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
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
    _crawler_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _crawler_runs: dict = field(default_factory=dict, repr=False)

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

    def rebuild_notifiers(self) -> None:
        """Rebuild the notifier list after webhook_url changed at runtime."""
        self.notifiers = _build_notifiers(self.settings)

    # --------------------------------------------------- crawler workers
    def start_crawler(self, name: str, crawler, now: datetime) -> bool:
        """Start one crawler in the background. Returns False if already up."""
        with self._crawler_lock:
            entry = self._crawler_runs.get(name)
            if entry and entry[0].is_alive():
                return False
            self._crawler_runs.pop(name, None)
            crawler.reset_stop()
            thread = threading.Thread(
                target=trigger.run_crawler, args=(self, name, crawler, now),
                name=f"omnifetch-crawler-{name}", daemon=True,
            )
            self._crawler_runs[name] = (thread, crawler)
            thread.start()
            return True

    def is_crawler_running(self, name: str) -> bool:
        with self._crawler_lock:
            entry = self._crawler_runs.get(name)
            return bool(entry and entry[0].is_alive())

    def finish_crawler(self, name: str) -> None:
        current = threading.current_thread()
        with self._crawler_lock:
            entry = self._crawler_runs.get(name)
            if entry and entry[0] is current:
                self._crawler_runs.pop(name, None)

    def wait_crawlers(self, timeout: float | None = None) -> None:
        """Join running crawler workers (used by `run --once` and tests)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._crawler_lock:
                threads = [t for t, _ in self._crawler_runs.values()]
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                return
            for thread in alive:
                if deadline is None:
                    thread.join()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    thread.join(remaining)

    def stop_crawlers(self, timeout: float = 30.0) -> None:
        """Ask every crawler to stop after its current batch, then join."""
        with self._crawler_lock:
            runs = list(self._crawler_runs.values())
        for _, crawler in runs:
            crawler.request_stop()
        deadline = time.monotonic() + timeout
        for thread, _ in runs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if thread.is_alive():
                thread.join(remaining)

    def cancel_download(self, downloader: str, external_id: str) -> None:
        """Best-effort stop of a downloader-side task (used by retry-next)."""
        instance = self.downloaders.get(downloader)
        if instance is None or not external_id:
            return
        try:
            instance.cancel(external_id)
        except Exception:
            log.warning("cancel failed for %s", external_id, exc_info=True)

    # ------------------------------------------------------- live reload
    def reload_source(self, name: str) -> str:
        """Rebuild one crawler from its (possibly edited) config file.
        Returns a short status string for the UI."""
        sources, _ = load_plugin_configs(self.settings.config_dir)
        cfg = sources.get(name)
        if cfg is None:
            self.crawlers.pop(name, None)
            return "配置已删除"
        if not cfg.get("enabled", True):
            self.crawlers.pop(name, None)
            return "已保存（停用）"
        self.crawlers[name] = create_crawler(cfg["type"], name, cfg)
        return "已保存并启用"

    def reload_downloader(self, name: str) -> str:
        _, downloaders = load_plugin_configs(self.settings.config_dir)
        cfg = downloaders.get(name)
        if cfg is None:
            self.downloaders.pop(name, None)
            return "配置已删除"
        if not cfg.get("enabled", True):
            self.downloaders.pop(name, None)
            return "已保存（停用）"
        self.downloaders[name] = create_downloader(cfg["type"], name, cfg)
        return "已保存并启用"


def _build_notifiers(settings: Settings) -> list[Notifier]:
    notifiers: list[Notifier] = [NullNotifier()]
    if settings.webhook_url:
        notifiers.append(WebhookNotifier(settings.webhook_url))
    return notifiers


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

    log.info(
        "context ready: %d crawlers, %d downloaders",
        len(crawlers), len(downloaders),
    )
    return Context(settings, crawlers, downloaders, _build_notifiers(settings))


def run_forever(ctx: Context, stop_event: threading.Event | None = None) -> None:
    """Run scheduler ticks until stop_event is set (CLI run / serve thread)."""
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                run_once(ctx)
            except Exception:
                log.exception("scheduler tick failed")
            if stop_event is None:
                time.sleep(ctx.settings.poll_interval)
            else:
                stop_event.wait(ctx.settings.poll_interval)
    finally:
        # Do not abandon a half-written crawl batch when the process exits.
        ctx.stop_crawlers()


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
