"""VCB-Studio crawler: Google Sheets index -> VCB release pages -> BT mirrors.

Pipeline per run (fetch_batches yields every ``batch_size`` rows so the
scheduler can ingest incrementally and stop between batches):
  1. Download the sheet as CSV through the configured proxy.
  2. For the first ``max_pages_per_run`` rows (sheet is newest-first), fetch
     the VCB release page (Cloudflare-protected; see cf_clearance/playwright
     below) and collect its mirror links.
  3. Resolve each mirror to actual download URIs:
       nyaa.si   /view/N -> /download/N.torrent   (pure URL rewrite, no fetch)
       dmhy      fetch page, extract magnet
       bangumi   fetch /api/torrent/<id>, extract magnet
     One Resource per mirror -> they become fallback candidates of the want.
  4. Politeness delay between VCB pages (interruptible via stop).

Cloudflare handling for vcb-s.com (managed challenge; cloudscraper and
TLS-impersonation alone do NOT pass):
  cf_clearance cookie copied from your browser (fast) — set
    cf_clearance + user_agent in the source config;
  or backend = "playwright" (slower, self-sufficient; needs the
    ``browser`` extra: pip install ".[browser]" + playwright install chromium).

Only mirrors reachable without a second challenge are resolved; blocked or
unknown mirrors are skipped with a warning, never fatal.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from typing import Iterator
from urllib.parse import urlparse

import httpx

from ..core.models import Resource, ResourceType
from . import register
from .base import CrawlBatch, CrawlError, CrawlInterrupted, CrawlerPlugin

log = logging.getLogger(__name__)

_SHEET_CSV = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid={gid}"
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{32,40}")
_NYAA_VIEW_RE = re.compile(r"https?://(?:www\.)?nyaa\.si/view/(\d+)")


@register
class VcbCrawler(CrawlerPlugin):
    type = "vcb"
    DEFAULT_CONFIG = {"sheet_id": "", "gid": "0", "interval": 1800,
                      "max_pages_per_run": 30, "batch_size": 20,
                      "delay": 2.0, "backend": "cookie",
                      "mirror_retries": 2, "mirror_backoff": 2.0}

    def fetch(self) -> list[Resource]:
        return [r for batch in self.fetch_batches() for r in batch.resources]

    def fetch_batches(self) -> Iterator[CrawlBatch]:
        proxy = self.config.get("proxy") or None
        max_pages = int(self.config.get("max_pages_per_run", 30))
        batch_size = max(1, int(self.config.get("batch_size", 20)))
        delay = float(self.config.get("delay", 2.0))

        rows = self._fetch_sheet(proxy)
        if max_pages > 0:
            rows = rows[:max_pages]
        total = len(rows)
        backend = self._make_backend()
        pending: list[Resource] = []
        done = 0
        attempted = 0
        succeeded = 0
        stopped = False

        try:
            for row in rows:
                if self.should_stop():
                    stopped = True
                    break
                link = row.get("link") or ""
                if link:
                    attempted += 1
                    try:
                        html = backend.fetch(link)
                    except Exception as exc:
                        log.warning("vcb page fetch failed %s: %s", link, exc)
                        html = None
                    if html is not None:
                        succeeded += 1
                        pending.extend(self._expand_row(row, html, proxy))
                done += 1
                if done % batch_size == 0:
                    yield CrawlBatch(pending, done, total)
                    pending = []
                if delay and not self.sleep(delay):
                    stopped = True
                    break
            if stopped:
                if pending:
                    yield CrawlBatch(pending, done, total)
                raise CrawlInterrupted(f"已处理 {done}/{total} 行后中止")
            # Every page failing is a source failure (expired cookie, CF block,
            # proxy down), not a legitimately empty sheet. Reporting success
            # here would silence the source for a whole interval.
            if attempted and not succeeded:
                raise CrawlError(
                    f"全部 {attempted} 个 VCB 页面获取失败（Cookie/Cloudflare/代理）")
            if pending or (done and done % batch_size):
                yield CrawlBatch(pending, done, total)
        finally:
            backend.close()

    # ------------------------------------------------------------- sheet

    def _fetch_sheet(self, proxy: str | None) -> list[dict]:
        sheet_id = self.config["sheet_id"]
        gid = self.config.get("gid", "0")
        url = _SHEET_CSV.format(sheet_id=sheet_id, gid=gid)
        with httpx.Client(proxy=proxy, timeout=30, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        # Column order: UID, Title (multiline), Info, Link, (empty)
        rows = []
        for record in csv.reader(io.StringIO(response.text)):
            if len(record) < 4 or record[0] == "UID":
                continue
            rows.append(
                {"uid": record[0].strip(), "title": record[1].strip(),
                 "info": record[2].strip(), "link": record[3].strip()}
            )
        return rows

    # ------------------------------------------------- page expansion

    def _expand_row(self, row: dict, page_html: str, proxy: str | None) -> list[Resource]:
        title = row["title"] or _page_title(page_html) or row["link"]
        resources = []
        for site, page_url in _extract_mirrors(page_html):
            uris, rtype = self._resolve_mirror(site, page_url, proxy)
            if not uris:
                continue
            resources.append(
                Resource(
                    title=title,
                    source_url=page_url,
                    source=f"vcb-{site}",
                    download_uris=uris,
                    resource_type=rtype,
                    extra={"vcb_uid": row["uid"], "vcb_info": row["info"]},
                )
            )
        if not resources:
            log.info("no usable mirrors on %s", row["link"])
        return resources

    def _resolve_mirror(self, site: str, page_url: str, proxy: str | None):
        """Return (uris, resource_type) or ([], None) when unreachable."""
        if site == "nyaa":
            # Pure URL rewrite: nyaa serves the .torrent at a fixed pattern
            # and the site is reachable without a challenge.
            match = _NYAA_VIEW_RE.match(page_url)
            if match:
                return [f"https://nyaa.si/download/{match.group(1)}.torrent"], ResourceType.TORRENT
        if site == "dmhy":
            html = _http_get(
                page_url, proxy,
                retries=int(self.config.get("mirror_retries", 2)),
                backoff=float(self.config.get("mirror_backoff", 2.0)),
            )
            if html:
                magnets = _MAGNET_RE.findall(html)
                if magnets:
                    return list(dict.fromkeys(magnets)), ResourceType.MAGNET
        log.warning("mirror %s not resolvable: %s", site, page_url)
        return [], None

    # ------------------------------------------------- cf page backend

    def _make_backend(self):
        if self.config.get("backend", "cookie") == "playwright":
            return _PlaywrightBackend(self.config)
        return _CookieBackend(self.config)


class _CookieBackend:
    """curl_cffi (Chrome TLS fingerprint) + cf_clearance cookie obtained
    manually from a real browser. The user_agent must be the UA of the
    browser session that solved the challenge."""

    def __init__(self, config: dict):
        from curl_cffi import requests as cffi

        proxy = config.get("proxy") or None
        self._session = cffi.Session(
            impersonate="chrome",
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        clearance = config.get("cf_clearance") or None
        if clearance:
            self._session.cookies.set("cf_clearance", clearance, domain="vcb-s.com")
        user_agent = config.get("user_agent") or None
        if user_agent:
            self._session.headers.update({"User-Agent": user_agent})

    def fetch(self, url: str) -> str | None:
        response = self._session.get(url, timeout=30)
        if response.status_code != 200:
            log.warning("vcb page %s -> http %s (cf clearance expired?)",
                        url, response.status_code)
            return None
        return response.text

    def close(self) -> None:
        self._session.close()


class _PlaywrightBackend:
    """Headless Chromium solving the managed challenge by itself."""

    def __init__(self, config: dict):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        proxy = config.get("proxy") or None
        self._browser = self._playwright.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
        )

    def fetch(self, url: str) -> str | None:
        page = self._browser.new_page()
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            for _ in range(3):  # managed challenge auto-resolves in seconds
                title = page.title()
                if "just a moment" in title.lower() or "请稍候" in title:
                    page.wait_for_timeout(6000)
                    page.reload(wait_until="domcontentloaded")
                else:
                    break
            return page.content()
        finally:
            page.close()

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()


# ----------------------------------------------------------------- helpers

_RETRY_STATUS = {429, 500, 502, 503, 504}


def _http_get(url: str, proxy: str | None, retries: int = 2,
              backoff: float = 2.0) -> str | None:
    """Fetch a mirror page, retrying transient 5xx/429 and network errors.

    dmhy regularly answers 502 for a while and then recovers; without a
    retry every such row loses its only magnet. Backoff grows exponentially
    (backoff, 2*backoff, ...), and 4xx other than 429 is not retried.
    """
    attempts = max(0, retries) + 1
    for attempt in range(attempts):
        try:
            with httpx.Client(proxy=proxy, timeout=30, follow_redirects=True) as client:
                response = client.get(url)
            status = response.status_code
            if status == 200:
                return response.text
            if status not in _RETRY_STATUS or attempt >= attempts - 1:
                log.warning("mirror fetch %s -> http %s", url, status)
                return None
            log.info("mirror fetch %s -> http %s, retry %d/%d",
                     url, status, attempt + 1, retries)
        except httpx.HTTPError as exc:
            if attempt >= attempts - 1:
                log.warning("mirror fetch %s failed: %s", url, exc)
                return None
            log.info("mirror fetch %s failed: %s, retry %d/%d",
                     url, exc, attempt + 1, retries)
        time.sleep(backoff * (2 ** attempt))
    return None


def _page_title(html: str) -> str | None:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    if match:
        return re.sub(r"<[^>]+>", "", match.group(1)).strip() or None
    return None


def _extract_mirrors(html: str) -> list[tuple[str, str]]:
    """Classify mirror links on a VCB release page as (site, page_url)."""
    hosts = {
        "nyaa.si": "nyaa",
        "share.dmhy.org": "dmhy",
        # bangumi.moe is a pure JS SPA with no public torrent API —
        # unreachable for a server-side crawler, skipped silently.
    }
    found: list[tuple[str, str]] = []
    for match in re.finditer(r'href=["\'](https?://[^"\']+)["\']', html):
        url = match.group(1)
        # nyaa publishes both nyaa.si and www.nyaa.si links.
        host = urlparse(url).netloc.lower().removeprefix("www.")
        site = hosts.get(host)
        if site and (site, url) not in found:
            found.append((site, url))
    return found
