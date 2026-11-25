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


def test_www_nyaa_mirror_is_recognized_and_resolved(crawler):
    page = '<a href="https://www.nyaa.si/view/42">nyaa</a>'
    assert vcb._extract_mirrors(page) == [("nyaa", "https://www.nyaa.si/view/42")]
    uris, rtype = crawler._resolve_mirror("nyaa", "https://www.nyaa.si/view/42", None)
    assert uris == ["https://nyaa.si/download/42.torrent"]
    assert rtype == ResourceType.TORRENT


def test_dmhy_page_magnet_extracted(crawler, monkeypatch):
    monkeypatch.setattr(vcb, "_http_get",
                        lambda url, proxy, **kw: DMHY_PAGE)
    uris, rtype = crawler._resolve_mirror(
        "dmhy", "https://share.dmhy.org/topics/view/730693_x.html", None
    )
    assert uris == ["magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01"]
    assert rtype == ResourceType.MAGNET


def test_fetch_emits_one_resource_per_resolvable_mirror(crawler, monkeypatch):
    monkeypatch.setattr(crawler, "_make_backend", lambda: FakeBackend(VCB_PAGE))
    monkeypatch.setattr(vcb, "_http_get",
                        lambda url, proxy, **kw: DMHY_PAGE)
    results = crawler.fetch()
    by_site = {r.source: r for r in results}
    assert set(by_site) == {"vcb-nyaa", "vcb-dmhy"}
    nyaa = by_site["vcb-nyaa"]
    assert nyaa.download_uris[0].endswith("2184807.torrent")
    assert "Guild no Uketsukejou" in nyaa.title or "Mayonaka Heart Tune" in nyaa.title
    assert nyaa.extra["vcb_uid"] in {"261102", "261101"}


def test_resources_carry_one_auto_want_per_sheet_row(crawler, monkeypatch):
    # One sheet row == one download: both mirrors of a row share a stable
    # want_key so the core creates a single want for the row.
    monkeypatch.setattr(crawler, "_make_backend", lambda: FakeBackend(VCB_PAGE))
    monkeypatch.setattr(vcb, "_http_get", lambda url, proxy, **kw: DMHY_PAGE)
    results = crawler.fetch()

    per_uid: dict[str, set[str]] = {}
    for res in results:
        per_uid.setdefault(res.extra["vcb_uid"], set()).add(res.extra["want_key"])
        assert res.extra["want_title"] == res.title
    assert per_uid == {"261102": {"vcb-261102"}, "261101": {"vcb-261101"}}


def test_total_rows_reported_before_first_page(crawler, monkeypatch):
    monkeypatch.setattr(crawler, "_make_backend", lambda: FakeBackend(VCB_PAGE))
    monkeypatch.setattr(vcb, "_http_get", lambda url, proxy, **kw: DMHY_PAGE)
    batches = list(crawler.fetch_batches())
    assert batches[0].resources == []
    assert batches[0].done == 0 and batches[0].total == 2


def test_all_pages_failed_raises_instead_of_empty_success(crawler, monkeypatch):
    # CF block / dead cookie: every page returns non-200. A silent [] would be
    # recorded as a successful run and silence the source for its interval.
    monkeypatch.setattr(crawler, "_make_backend", lambda: FakeBackend(None))
    with pytest.raises(vcb.CrawlError):
        crawler.fetch()


def test_partial_page_failure_keeps_successful_rows(crawler, monkeypatch):
    class _SequenceBackend:
        def __init__(self):
            self._htmls = [VCB_PAGE, None]

        def fetch(self, url):
            return self._htmls.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(crawler, "_make_backend", lambda: _SequenceBackend())
    monkeypatch.setattr(vcb, "_http_get",
                        lambda url, proxy, **kw: DMHY_PAGE)
    results = crawler.fetch()
    assert results                       # first row still produced resources


def test_consecutive_page_failures_trip_the_circuit_breaker(crawler, monkeypatch):
    # A dead cookie / CF challenge fails every page. Crawling all ~1100 rows
    # would hammer the site for hours, so abort early and let the source back
    # off — batches already yielded must stay ingested.
    rows = [{"uid": str(i), "title": f"T{i}", "info": "", "link":
             f"https://vcb-s.com/archives/{i}"} for i in range(20)]

    class _SequenceBackend:
        def __init__(self):
            self._htmls = [VCB_PAGE] + [None] * 19
            self.calls = 0

        def fetch(self, url):
            self.calls += 1
            return self._htmls.pop(0)

        def close(self):
            pass

    backend = _SequenceBackend()
    monkeypatch.setattr(crawler, "_fetch_sheet", lambda proxy: rows)
    monkeypatch.setattr(crawler, "_make_backend", lambda: backend)
    monkeypatch.setattr(vcb, "_http_get", lambda url, proxy, **kw: DMHY_PAGE)
    crawler.config.update({"max_consecutive_failures": 3, "batch_size": 1, "delay": 0})

    batches = []
    with pytest.raises(vcb.CrawlError, match="连续 3 个"):
        for batch in crawler.fetch_batches():
            batches.append(batch)

    assert any(b.resources for b in batches)      # first row still ingested
    assert backend.calls == 1 + 3                 # stopped after the threshold


def test_failure_counter_resets_after_a_successful_page(crawler, monkeypatch):
    class _FlakyBackend:
        def __init__(self):
            # fail, fail, succeed, fail, fail, succeed -> never 3 in a row
            self._htmls = [None, None, VCB_PAGE, None, None, VCB_PAGE]

        def fetch(self, url):
            return self._htmls.pop(0)

        def close(self):
            pass

    rows = [{"uid": str(i), "title": f"T{i}", "info": "", "link":
             f"https://vcb-s.com/archives/{i}"} for i in range(6)]
    monkeypatch.setattr(crawler, "_fetch_sheet", lambda proxy: rows)
    monkeypatch.setattr(crawler, "_make_backend", lambda: _FlakyBackend())
    monkeypatch.setattr(vcb, "_http_get", lambda url, proxy, **kw: DMHY_PAGE)
    crawler.config.update({"max_consecutive_failures": 3, "batch_size": 1, "delay": 0})

    resources = crawler.fetch()
    assert resources                                  # completed, no CrawlError


def test_empty_sheet_is_reported_as_error(crawler, monkeypatch):
    monkeypatch.setattr(crawler, "_fetch_sheet", lambda proxy: [])
    monkeypatch.setattr(crawler, "_make_backend", lambda: FakeBackend(None))
    with pytest.raises(vcb.CrawlError, match="0 行"):
        crawler.fetch()


class _SheetClient:
    def __init__(self, text: str):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        return _FakeResponse(200, self._text)


def test_html_200_response_is_rejected_as_invalid_csv(monkeypatch):
    # Google sometimes answers 200 with a login/error HTML page; parsing that
    # must not look like a valid empty sheet.
    raw = vcb.VcbCrawler("vcb", {"sheet_id": "x"})
    monkeypatch.setattr(vcb.httpx, "Client", lambda **kw: _SheetClient("<html>sign in</html>"))
    with pytest.raises(vcb.CrawlError, match="表头"):
        raw._fetch_sheet(None)


def test_header_only_sheet_is_reported_as_error(monkeypatch):
    raw = vcb.VcbCrawler("vcb", {"sheet_id": "x"})
    header = '"UID","Romanji Title / 项目名","Info / 备注","Link / 链接",""\n'
    monkeypatch.setattr(vcb.httpx, "Client", lambda **kw: _SheetClient(header))
    with pytest.raises(vcb.CrawlError, match="0 行"):
        raw._fetch_sheet(None)


def test_valid_sheet_header_and_rows_still_parse(monkeypatch):
    raw = vcb.VcbCrawler("vcb", {"sheet_id": "x"})
    monkeypatch.setattr(vcb.httpx, "Client", lambda **kw: _SheetClient(SHEET_CSV))
    rows = raw._fetch_sheet(None)
    assert len(rows) == 2 and rows[0]["uid"] == "261102"


class _FakeResponse:
    def __init__(self, code: int, text: str = "page"):
        self.status_code = code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_client(sequence: list):
    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return _FakeResponse(item)

    return _Client


def test_mirror_fetch_retries_502_with_exponential_backoff(monkeypatch):
    calls: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(vcb.httpx, "Client", _fake_client([502, 502, 200]))
    monkeypatch.setattr(vcb.time, "sleep", sleeps.append)

    assert vcb._http_get("u", None, retries=2, backoff=2.0) == "page"
    assert sleeps == [2.0, 4.0]


def test_mirror_fetch_retries_network_errors(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(vcb.httpx, "Client",
                        _fake_client([vcb.httpx.ConnectError("boom"), 200]))
    monkeypatch.setattr(vcb.time, "sleep", sleeps.append)

    assert vcb._http_get("u", None, retries=1, backoff=1.5) == "page"
    assert sleeps == [1.5]


def test_mirror_fetch_gives_up_after_retries(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(vcb.httpx, "Client", _fake_client([502, 502, 502]))
    monkeypatch.setattr(vcb.time, "sleep", sleeps.append)

    assert vcb._http_get("u", None, retries=2, backoff=2.0) is None
    assert sleeps == [2.0, 4.0]


def test_mirror_fetch_does_not_retry_404(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(vcb.httpx, "Client", _fake_client([404]))
    monkeypatch.setattr(vcb.time, "sleep", sleeps.append)

    assert vcb._http_get("u", None, retries=2) is None
    assert sleeps == []


def test_dmhy_resolution_uses_configured_retry_policy(crawler, monkeypatch):
    seen = {}

    def fake_get(url, proxy, **kw):
        seen.update(kw)
        return DMHY_PAGE

    monkeypatch.setattr(vcb, "_http_get", fake_get)
    crawler.config["mirror_retries"] = 3
    crawler.config["mirror_backoff"] = 0.5
    crawler._resolve_mirror("dmhy", "https://share.dmhy.org/topics/view/1_x.html", None)
    assert seen == {"retries": 3, "backoff": 0.5}
