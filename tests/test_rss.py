"""RSS crawler: enclosure-first URI ordering and metadata extraction."""
from __future__ import annotations

import feedparser
import pytest

from omnifetch.crawlers.rss import RssCrawler
from omnifetch.core.models import ResourceType

FEED_WITH_ENCLOSURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>demo</title>
<item>
  <title>Some Show S01E02 [1080p]</title>
  <link>https://site.example/post/2</link>
  <enclosure url="https://cdn.example/2.torrent" length="123456"
             type="application/x-bittorrent"/>
  <pubDate>Mon, 24 Nov 2025 10:00:00 GMT</pubDate>
</item>
</channel></rss>
"""

FEED_LINK_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>demo</title>
<item>
  <title>Another Show 720p</title>
  <link>https://site.example/post/3</link>
</item>
</channel></rss>
"""


def _crawler(monkeypatch, feed: str, **cfg) -> RssCrawler:
    parsed = feedparser.parse(feed)
    monkeypatch.setattr("omnifetch.crawlers.rss.feedparser.parse", lambda url: parsed)
    return RssCrawler("demo-rss", {"url": "https://site.example/feed.xml", **cfg})


def test_enclosure_uri_comes_before_link(monkeypatch):
    crawler = _crawler(monkeypatch, FEED_WITH_ENCLOSURE,
                       resource_type="torrent", want_key="some show")
    resource = crawler.fetch()[0]
    assert resource.download_uris == [
        "https://cdn.example/2.torrent",
        "https://site.example/post/2",
    ]
    assert resource.extra["size"] == 123456
    assert resource.extra["want_key"] == "some show"
    assert resource.resource_type == ResourceType.TORRENT
    assert resource.published_at is not None


def test_link_only_entry_still_yields_download_uri(monkeypatch):
    resource = _crawler(monkeypatch, FEED_LINK_ONLY).fetch()[0]
    assert resource.download_uris == ["https://site.example/post/3"]
    assert "size" not in resource.extra
    assert resource.resource_type == ResourceType.HTTP


def test_duplicate_enclosure_and_link_are_deduplicated(monkeypatch):
    feed = FEED_WITH_ENCLOSURE.replace("https://site.example/post/2",
                                       "https://cdn.example/2.torrent")
    resource = _crawler(monkeypatch, feed).fetch()[0]
    assert resource.download_uris == ["https://cdn.example/2.torrent"]
