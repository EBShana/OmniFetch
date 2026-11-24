"""P115 (115 网盘离线下载) plugin tests with a stubbed API."""
from __future__ import annotations

import hashlib
import json

import pytest

from omnifetch.core.models import ErrorKind, Resource, ResourceType
from omnifetch.downloaders.base import DownloadError, PollStatus
from omnifetch.downloaders.p115 import P115Api, P115Downloader, torrent_infohash

INFO = b"d4:name4:test6:lengthi4ee"
TORRENT = b"d8:announce16:udp://tracker:804:info" + INFO + b"e"
DIGEST = hashlib.sha1(INFO).hexdigest()


class FakeApi:
    def __init__(self, tasks=None, add_error=None):
        self.tasks = tasks or []
        self.add_error = add_error
        self.added_urls: list[str] = []
        self.added_bts: list[str] = []
        self.deleted: list[str] = []

    def add_task_url(self, url, wp_path_id="0", savepath=""):
        if self.add_error:
            raise self.add_error
        self.added_urls.append(url)
        return {"state": True}

    def add_task_bt(self, info_hash, wp_path_id="0"):
        if self.add_error:
            raise self.add_error
        self.added_bts.append(info_hash)
        return {"state": True}

    def task_lists(self, page=1, page_size=30, stat=0):
        return {"state": True, "tasks": self.tasks if page == 1 else [],
                "page": page, "page_count": 1, "count": len(self.tasks)}

    def task_del(self, info_hash):
        self.deleted.append(info_hash)


def make(api: FakeApi, **cfg) -> P115Downloader:
    dl = P115Downloader("p115", {"cookie_file": "x.json", **cfg})
    dl._api = api
    return dl


def resource(uri: str, rtype: ResourceType) -> Resource:
    return Resource(title="t", source_url=uri, source="s",
                    download_uris=[uri], resource_type=rtype)


# ------------------------------------------------------------- infohash

def test_torrent_infohash():
    assert torrent_infohash(TORRENT) == DIGEST


def test_torrent_infohash_rejects_non_torrent():
    with pytest.raises(ValueError):
        torrent_infohash(b"not a torrent")


# --------------------------------------------------------------- submit

def test_submit_magnet_uses_url_api():
    api = FakeApi()
    dl = make(api)
    key = dl.submit(resource(
        "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        ResourceType.MAGNET))
    assert key == "hash:abcdef0123456789abcdef0123456789abcdef01"
    assert len(api.added_urls) == 1 and not api.added_bts


def test_submit_torrent_parses_infohash_and_uses_bt_api():
    api = FakeApi()
    dl = make(api)
    dl._fetch_torrent = lambda url: TORRENT  # stub network
    key = dl.submit(resource("https://nyaa.si/download/1.torrent", ResourceType.TORRENT))
    assert key == f"hash:{DIGEST}"
    assert api.added_bts == [DIGEST]


def test_submit_plain_http_gets_url_key():
    api = FakeApi()
    dl = make(api)
    key = dl.submit(resource("https://example.com/file.zip", ResourceType.HTTP))
    assert key == "url:https://example.com/file.zip"


def test_submit_duplicate_is_idempotent():
    api = FakeApi(tasks=[{"info_hash": "aa" * 20, "url": "",
                          "percent": 50, "status": 0}],
                  add_error=DownloadError("115 添加离线任务: 任务已存在",
                                          ErrorKind.PERMANENT))
    dl = make(api)
    key = dl.submit(resource("magnet:?xt=urn:btih:" + "AA" * 20, ResourceType.MAGNET))
    assert key == "hash:" + "aa" * 20  # existing task reused, no exception


def test_submit_no_uris_permanent():
    dl = make(FakeApi())
    bad = resource("", ResourceType.HTTP)
    with pytest.raises(DownloadError) as exc_info:
        dl.submit(bad)
    assert exc_info.value.kind == ErrorKind.PERMANENT


# ----------------------------------------------------------------- poll

def _task(**kw):
    base = {"info_hash": "ab" * 20, "url": "", "percent": 0, "status": 0,
            "fail_msg": "", "available_actions": []}
    base.update(kw)
    return base


def test_poll_progress_done_and_failed():
    dl = make(FakeApi(tasks=[_task(percent=42.5)]))
    result = dl.poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.PROGRESS and abs(result.progress - 0.425) < 1e-6

    dl = make(FakeApi(tasks=[_task(percent=100, status=1)]))
    assert dl.poll("hash:" + "ab" * 20).status == PollStatus.DONE

    dl = make(FakeApi(tasks=[_task(fail_msg="找不到文件")]))
    result = dl.poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT
    assert "找不到文件" in result.error_message


def test_poll_missing_task_is_permanent_failure():
    dl = make(FakeApi(tasks=[]))
    result = dl.poll("hash:" + "cc" * 20)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT


def test_poll_matches_by_url_key():
    dl = make(FakeApi(tasks=[_task(info_hash="", url="https://x/f.zip", percent=10)]))
    result = dl.poll("url:https://x/f.zip")
    assert result.status == PollStatus.PROGRESS


# ---------------------------------------------------------- exists/cancel

def test_exists_matches_by_hash_or_url():
    dl = make(FakeApi(tasks=[_task()]))
    assert dl.exists(resource("magnet:?xt=urn:btih:" + "AB" * 20, ResourceType.MAGNET))
    dl2 = make(FakeApi(tasks=[_task(info_hash="", url="https://x/f.zip")]))
    assert dl2.exists(resource("https://x/f.zip", ResourceType.HTTP))
    dl3 = make(FakeApi(tasks=[]))
    assert not dl3.exists(resource("magnet:?xt=urn:btih:" + "FF" * 20, ResourceType.MAGNET))


def test_cancel_deletes_by_hash():
    api = FakeApi()
    dl = make(api)
    dl.cancel("hash:" + "ab" * 20)
    assert api.deleted == ["ab" * 20]


# ------------------------------------------------------------------ ping

def test_ping_reports_online_and_offline():
    dl = make(FakeApi(tasks=[_task()]))
    ok, detail = dl.ping()
    assert ok and "1" in detail

    api = FakeApi()
    api.task_lists = lambda **kw: (_ for _ in ()).throw(
        DownloadError("115 Cookie 已失效", ErrorKind.PERMANENT))
    ok, detail = make(api).ping()
    assert not ok and "Cookie" in detail


# ------------------------------------------------------------- cookies

def test_cookie_file_roundtrip(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"UID": "u1", "CID": "c1", "SEID": "s1"}),
                    encoding="utf-8")
    api = P115Api(str(path))
    assert api._client.cookies.get("UID") == "u1"
    api._client.cookies.set("KID", "k1", domain=".115.com")
    api.save_cookies()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {"UID": "u1", "CID": "c1", "SEID": "s1", "KID": "k1"}


def test_submit_without_cookie_config_is_permanent():
    dl = P115Downloader("p115", {})
    with pytest.raises(DownloadError) as exc_info:
        dl.submit(resource("magnet:?xt=urn:btih:" + "AA" * 20, ResourceType.MAGNET))
    assert exc_info.value.kind == ErrorKind.PERMANENT
    assert "115-login" in str(exc_info.value)
