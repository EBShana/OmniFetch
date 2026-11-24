from __future__ import annotations

from datetime import datetime, timezone
from time import mktime

import feedparser

from ..core.models import Resource, ResourceType
from . import register
from .base import CrawlerPlugin


@register
class RssCrawler(CrawlerPlugin):
    """Generic RSS/Atom source. Config keys:

      url           feed URL (required)
      source        site identifier stored on resources (default: instance name)
      resource_type http | magnet | torrent (default: http)
      want_key      subscription mode: work_key of the want candidates belong to
    """

    type = "rss"

    def fetch(self) -> list[Resource]:
        feed = feedparser.parse(self.config["url"])
        resource_type = ResourceType(self.config.get("resource_type", "http"))
        source = self.config.get("source", self.name)
        want_key = self.config.get("want_key") or ""

        results: list[Resource] = []
        for entry in feed.entries:
            link = getattr(entry, "link", "")

            # An enclosure is the actual media/download file; the plain link
            # is usually just the article page, so it only acts as fallback.
            enclosure_uris: list[str] = []
            size = None
            for enclosure in getattr(entry, "enclosures", []) or []:
                href = enclosure.get("href")
                if href:
                    enclosure_uris.append(href)
                length = enclosure.get("length")
                if length and str(length).isdigit():
                    size = int(length)

            uris = list(dict.fromkeys(enclosure_uris + ([link] if link else [])))
            if not uris:
                continue

            published = None
            parsed = getattr(entry, "published_parsed", None)
            if parsed:
                published = datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)

            extra: dict = {}
            if want_key:
                extra["want_key"] = want_key
            if size:
                extra["size"] = size

            results.append(
                Resource(
                    title=(entry.get("title") or "").strip(),
                    source_url=link,
                    source=source,
                    download_uris=uris,
                    resource_type=resource_type,
                    published_at=published,
                    extra=extra,
                )
            )
        return results
