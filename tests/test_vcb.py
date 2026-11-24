"""VCB crawler unit tests: sheet parsing, mirror extraction/resolution.

All network access is stubbed; live behavior is covered by scripts/it_vcb.py.
"""
from __future__ import annotations

import csv
import io

import pytest

from omnifetch.core.models import ResourceType
from omnifetch.crawlers import vcb

SHEET_CSV = '''"UID","Romanji Title / 项目名","Info / 备注","Link / 链接",""
"261102","Guild no Uketsukejou Desu ga, Zangyou wa Iya nano de Boss o Solo Toubatsu Shiyou to Omoimasu\n虽然是公会的柜台小姐","TV","https://vcb-s.com/archives/21814",""
"261101","Mayonaka Heart Tune\n午夜的倾心旋律","TV(S1)","https://vcb-s.com/archives/21805",""
'''

VCB_PAGE = """
<html><head><title>release</title></head><body>
<a href="https://bangumi.moe/torrent/6af3eb5d992853e4c93cc18d">bangumi</a>
<a href="https://share.dmhy.org/topics/view/730693_x.html">dmhy</a>
<a href="https://nyaa.si/view/2184807">nyaa</a>
<a href="https://www.acgnx.se/show-df9aa2ca9446ded88704c067bd3543f328a87e1b.html">acgnx</a>
<a href="https://vcb-s.com/archives/21814">self</a>
</body></html>
"""

DMHY_PAGE = '<html><body><a href="magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01">mag</a></body></html>'


class FakeBackend:
    def __init__(self, html):
        self._html = html
        self.fetched: list[str] = []

    def fetch(self, url):
        self.fetched.append(url)
        return self._html

    def close(self):
        pass


@pytest.fixture
def crawler(monkeypatch):
    rows = [
        {"uid": r[0], "title": r[1], "info": r[2], "link": r[3]}
        for r in csv.reader(io.StringIO(SHEET_CSV))
        if r[0] != "UID"
    ]
    inst = vcb.VcbCrawler("vcb", {
        "sheet_id": "x", "gid": "0", "proxy": "http://p:7897",
        "max_pages_per_run": 10, "delay": 0,
    })
    monkeypatch.setattr(inst, "_fetch_sheet", lambda proxy: rows)
    return inst


def test_sheet_row_parsing(crawler):
    rows = crawler._fetch_sheet(None)
    assert len(rows) == 2
    assert rows[0]["uid"] == "261102"
    assert rows[0]["link"] == "https://vcb-s.com/archives/21814"
    assert "虽然是公会的柜台小姐" in rows[0]["title"]


def test_mirror_extraction_classifies_known_hosts():
    mirrors = vcb._extract_mirrors(VCB_PAGE)
    sites = {site for site, _ in mirrors}
    # bangumi (SPA) and acgnx (CF-challenged) are deliberately ignored
    assert sites == {"nyaa", "dmhy"}
    assert len(mirrors) == len(set(mirrors))     # no duplicates


def test_nyaa_view_url_rewritten_to_direct_torrent(crawler):
    uris, rtype = crawler._resolve_mirror(
        "nyaa", "https://nyaa.si/view/2184807", None
    )
    assert uris == ["https://nyaa.si/download/2184807.torrent"]
    assert rtype == ResourceType.TORRENT


def test_dmhy_page_magnet_extracted(crawler, monkeypatch):
    monkeypatch.setattr(vcb, "_http_get", lambda url, proxy: DMHY_PAGE)
    uris, rtype = crawler._resolve_mirror(
        "dmhy", "https://share.dmhy.org/topics/view/730693_x.html", None
    )
    assert uris == ["magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01"]
    assert rtype == ResourceType.MAGNET


def test_fetch_emits_one_resource_per_resolvable_mirror(crawler, monkeypatch):
    monkeypatch.setattr(crawler, "_make_backend", lambda: FakeBackend(VCB_PAGE))
    monkeypatch.setattr(vcb, "_http_get", lambda url, proxy: DMHY_PAGE)
    results = crawler.fetch()
    by_site = {r.source: r for r in results}
    assert set(by_site) == {"vcb-nyaa", "vcb-dmhy"}
    nyaa = by_site["vcb-nyaa"]
    assert nyaa.download_uris[0].endswith("2184807.torrent")
    assert "Guild no Uketsukejou" in nyaa.title or "Mayonaka Heart Tune" in nyaa.title
    assert nyaa.extra["vcb_uid"] in {"261102", "261101"}
