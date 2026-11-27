"""P115 (115 网盘离线下载) plugin tests with a stubbed API."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time

import pytest

from omnifetch.core.models import ErrorKind, Resource, ResourceType
from omnifetch.downloaders import p115
from omnifetch.downloaders.base import DownloadError, PollStatus
from omnifetch.downloaders.p115 import (
    P115Api,
    P115Downloader,
    btih_hex,
    torrent_infohash,
)

INFO = b"d4:name4:test6:lengthi4ee"
TORRENT = b"d8:announce16:udp://tracker:804:info" + INFO + b"e"
DIGEST = hashlib.sha1(INFO).hexdigest()


class FakeApi:
    def __init__(self, tasks=None, add_error=None, pages=None, return_hash=""):
        self.tasks = tasks or []
        self.add_error = add_error
        self.pages = pages  # {page: [task, ...]}；None 表示只有一页
        self.return_hash = return_hash  # 模拟 115 添加任务响应里的 info_hash
        self.calls = 0
        self.cookie_expired = False
        self.added_urls: list[str] = []
        self.added_url_savepaths: list[str | None] = []
        self.added_bts: list[str] = []
        self.added_bt_savepaths: list[str | None] = []
        self.deleted: list[str] = []

    def _added(self) -> dict:
        return {"state": True, "info_hash": self.return_hash} if self.return_hash \
            else {"state": True}

    def add_task_url(self, url, wp_path_id="0", savepath=None):
        if self.add_error:
            raise self.add_error
        self.added_urls.append(url)
        self.added_url_savepaths.append(savepath)
        return self._added()

    def add_task_bt(self, info_hash, wp_path_id="0", savepath=None):
        if self.add_error:
            raise self.add_error
        self.added_bts.append(info_hash)
        self.added_bt_savepaths.append(savepath)
        return self._added()

    def refresh_cookies(self):
        return False

    def task_lists(self, page=1, page_size=30, stat=0):
        self.calls += 1
        if self.pages is not None:
            return {"state": True, "tasks": self.pages.get(page, []),
                    "page": page, "page_count": len(self.pages),
                    "count": sum(len(t) for t in self.pages.values())}
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
    dl = make(api, target_dir_id="123", savepath="anime/2025")
    key = dl.submit(resource(
        "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        ResourceType.MAGNET))
    assert key == "hash:abcdef0123456789abcdef0123456789abcdef01"
    assert len(api.added_urls) == 1 and not api.added_bts
    # 磁链不传 savepath，让 115 按种子名自动建目录
    assert api.added_url_savepaths == [None]


def test_btih_hex_normalizes_base32_and_hex():
    raw = bytes.fromhex("ab" * 20)
    b32 = base64.b32encode(raw).decode()
    assert btih_hex(b32) == "ab" * 20
    assert btih_hex("AB" * 20) == "ab" * 20
    assert btih_hex("not-a-hash") == "not-a-hash"


def test_submit_base32_magnet_returns_hex_key():
    raw = bytes.fromhex("ab" * 20)
    b32 = base64.b32encode(raw).decode()
    api = FakeApi()
    key = make(api).submit(resource(f"magnet:?xt=urn:btih:{b32}", ResourceType.MAGNET))
    assert key == "hash:" + "ab" * 20
    assert api.added_urls == [f"magnet:?xt=urn:btih:{b32}"]


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


def test_submit_http_uses_info_hash_returned_by_115():
    # 115 部分链接会返回 info_hash：用它做键，cancel 才能按 hash 真正删除
    api = FakeApi(return_hash="AB" * 20)
    dl = make(api)
    key = dl.submit(resource("https://example.com/file.zip", ResourceType.HTTP))
    assert key == "hash:" + "ab" * 20
    dl.cancel(key)
    assert api.deleted == ["ab" * 20]


def test_submit_magnet_prefers_returned_hash():
    api = FakeApi(return_hash="CD" * 20)
    key = make(api).submit(resource(
        "magnet:?xt=urn:btih:" + "AB" * 20, ResourceType.MAGNET))
    assert key == "hash:" + "cd" * 20


def test_submit_torrent_omits_savepath():
    api = FakeApi()
    dl = make(api, target_dir_id="123", savepath="anime/2025")
    dl._fetch_torrent = lambda url: TORRENT  # stub network
    dl.submit(resource("https://nyaa.si/download/1.torrent", ResourceType.TORRENT))
    # 种子任务只传 wp_path_id，不传 savepath
    assert api.added_bt_savepaths == [None]


def test_submit_http_passes_savepath():
    api = FakeApi()
    dl = make(api, target_dir_id="123", savepath="anime/2025")
    dl.submit(resource("https://example.com/file.zip", ResourceType.HTTP))
    # 普通链接任务仍按配置传 savepath
    assert api.added_url_savepaths == ["anime/2025"]


def test_submit_duplicate_is_idempotent():
    api = FakeApi(tasks=[{"info_hash": "aa" * 20, "url": "",
                          "percentDone": 50, "status": 1}],
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
    base = {"info_hash": "ab" * 20, "url": "", "percentDone": 0, "status": 0,
            "display_status": "", "status_text": "", "available_actions": []}
    base.update(kw)
    return base


def test_poll_zero_percent_is_remote_queued():
    # 115 云端排队时 percentDone 一直是 0：要标成 queued，避免被核心判停滞。
    result = make(FakeApi(tasks=[_task(percentDone=0)])).poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.PROGRESS
    assert result.queued is True

    result = make(FakeApi(tasks=[_task(percentDone=42)])).poll("hash:" + "ab" * 20)
    assert result.queued is False


def test_poll_progress_done_and_failed():
    dl = make(FakeApi(tasks=[_task(percentDone=42.5)]))
    result = dl.poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.PROGRESS and abs(result.progress - 0.425) < 1e-6

    dl = make(FakeApi(tasks=[_task(percentDone=100, status=2,
                                   display_status="finished",
                                   status_text="下载成功")]))
    assert dl.poll("hash:" + "ab" * 20).status == PollStatus.DONE

    dl = make(FakeApi(tasks=[_task(status_text="下载失败：找不到文件",
                                   retry_count=3, retry_limit=3)]))
    result = dl.poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT
    assert "找不到文件" in result.error_message


def test_poll_real_finished_payload_is_done():
    # 2026-11 真机返回的已完成任务形态（字段名与取值为实际观测）
    task = {
        "info_hash": "ab" * 20, "url": "https://x/f.zip",
        "status": 2, "display_status": "finished", "status_text": "下载成功",
        "percentDone": 100, "display_percent": 100,
        "available_actions": ["view", "copy", "delete"],
        "retry_count": 0, "retry_limit": 3,
    }
    result = make(FakeApi(tasks=[task])).poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.DONE
    assert result.progress == 1.0


def test_poll_real_waiting_payload_is_queued():
    # 真机等待态：status=1、display_status=waiting、percentDone=0
    task = {
        "info_hash": "ab" * 20, "url": "https://x/f.zip",
        "status": 1, "display_status": "waiting", "status_text": "等待中",
        "percentDone": 0, "display_percent": 0,
        "available_actions": ["pause", "copy", "delete"],
    }
    result = make(FakeApi(tasks=[task])).poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.PROGRESS
    assert result.queued is True


def test_poll_real_failed_payload_is_retryable():
    # 真机失败态：status=-1、display_status=failed、retry_count=0 < retry_limit=3
    task = {
        "info_hash": "ab" * 20, "url": "https://x/dead.bin",
        "status": -1, "display_status": "failed", "status_text": "下载失败",
        "percentDone": 0, "display_percent": 0,
        "available_actions": ["copy", "delete"],
        "retry_count": 0, "retry_limit": 3,
    }
    api = FakeApi(tasks=[task])
    result = make(api).poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.TRANSIENT
    assert "下载失败" in result.error_message
    # 重试前先清掉云端失败任务
    assert api.deleted == ["ab" * 20]


def test_poll_real_failed_payload_at_limit_is_permanent():
    task = {
        "info_hash": "ab" * 20, "url": "https://x/dead.bin",
        "status": -1, "display_status": "failed", "status_text": "下载失败",
        "percentDone": 0, "available_actions": ["copy", "delete"],
        "retry_count": 3, "retry_limit": 3,
    }
    api = FakeApi(tasks=[task])
    result = make(api).poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT
    assert api.deleted == []


def test_poll_failed_without_hash_logs_and_still_retries(caplog):
    task = _task(info_hash="", url="https://x/f.zip", status=-1,
                 display_status="failed", status_text="下载失败",
                 retry_count=0, retry_limit=3)
    api = FakeApi(tasks=[task])
    dl = make(api)
    with caplog.at_level(logging.WARNING, logger="omnifetch.downloaders.p115"):
        result = dl.poll("url:https://x/f.zip")
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.TRANSIENT
    assert api.deleted == []
    assert any("info_hash" in record.getMessage() for record in caplog.records)


def test_poll_missing_task_is_permanent_failure():
    dl = make(FakeApi(tasks=[]))
    result = dl.poll("hash:" + "cc" * 20)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT


def test_poll_just_after_submit_uses_grace_period():
    # 115 列表入库有延迟：刚提交的任务在宽限期内不能判永久失败。
    dl = make(FakeApi(tasks=[]), submit_grace_seconds=60)
    key = dl.submit(resource("magnet:?xt=urn:btih:" + "AA" * 20, ResourceType.MAGNET))
    result = dl.poll(key)
    assert result.status == PollStatus.PROGRESS
    assert result.queued is True


def test_poll_after_grace_period_is_permanent():
    dl = make(FakeApi(tasks=[]), submit_grace_seconds=60)
    now = [1000.0]
    dl._now = lambda: now[0]
    key = dl.submit(resource("magnet:?xt=urn:btih:" + "AA" * 20, ResourceType.MAGNET))
    now[0] += 61
    result = dl.poll(key)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT


def test_task_found_clears_submit_grace():
    dl = make(FakeApi(tasks=[_task(percentDone=10)]), submit_grace_seconds=60)
    key = "hash:" + "ab" * 20
    dl._submitted[key] = dl._now()
    assert dl.poll(key).status == PollStatus.PROGRESS
    assert key not in dl._submitted


def test_poll_matches_by_url_key():
    dl = make(FakeApi(tasks=[_task(info_hash="", url="https://x/f.zip", percentDone=10)]))
    result = dl.poll("url:https://x/f.zip")
    assert result.status == PollStatus.PROGRESS


def test_poll_scans_all_pages_not_just_first():
    api = FakeApi(pages={1: [_task(info_hash="ee" * 20)], 2: [_task(percentDone=30)]})
    result = make(api).poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.PROGRESS
    assert abs(result.progress - 0.3) < 1e-6


def test_incomplete_scan_is_not_permanent_failure():
    pages = {page: [_task(info_hash="ee" * 20)] for page in range(1, 6)}
    dl = make(FakeApi(pages=pages))
    dl._max_scan_pages = 2
    result = dl.poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.PROGRESS
    assert result.queued is True


def test_task_list_snapshot_is_reused_within_ttl():
    api = FakeApi(tasks=[_task(info_hash="ab" * 20), _task(info_hash="cd" * 20)])
    dl = make(api, snapshot_ttl=10)
    now = [100.0]
    dl._now = lambda: now[0]
    dl.poll("hash:" + "ab" * 20)
    dl.poll("hash:" + "cd" * 20)
    assert api.calls == 1
    now[0] += 11
    dl.poll("hash:" + "ab" * 20)
    assert api.calls == 2


def test_submit_invalidates_task_list_snapshot():
    api = FakeApi(tasks=[_task(info_hash="ab" * 20)])
    dl = make(api, snapshot_ttl=60)
    dl.poll("hash:" + "ab" * 20)
    dl.submit(resource("magnet:?xt=urn:btih:" + "AA" * 20, ResourceType.MAGNET))
    dl.poll("hash:" + "cc" * 20)
    assert api.calls == 2


def test_submit_duplicate_found_on_later_page():
    api = FakeApi(
        pages={1: [_task(info_hash="ee" * 20)], 2: [_task(info_hash="aa" * 20)]},
        add_error=DownloadError("115 添加离线任务: 任务已存在", ErrorKind.PERMANENT),
    )
    key = make(api).submit(
        resource("magnet:?xt=urn:btih:" + "AA" * 20, ResourceType.MAGNET))
    assert key == "hash:" + "aa" * 20


# ------------------------------------------------------------- request API


def test_task_del_posts_hash0_with_signature():
    api = P115Api("missing.json")
    calls: dict = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"state": True}

    class _Client:
        def request(self, method, url, **kw):
            calls.update(method=method, url=url, **kw)
            return _Response()

    api._client.close()
    api._client = _Client()
    api._sign = ("sig", "1700000000")
    api._sign_at = time.time()

    api.task_del("ab" * 20)
    assert calls["method"] == "POST"
    assert calls["params"] == {"ac": "task_del"}
    assert calls["data"] == {"hash[0]": "ab" * 20, "sign": "sig",
                             "time": "1700000000"}


def test_add_tasks_post_to_web_endpoint():
    api = P115Api("missing.json")
    calls: list[dict] = []

    class _Response:
        status_code = 200

        def json(self):
            return {"state": True, "info_hash": "b82874bef1dcaabfc9ee2a24cee91646115"}

    class _Client:
        def request(self, method, url, **kw):
            calls.append({"method": method, "url": url, **kw})
            return _Response()

    api._client.close()
    api._client = _Client()

    api.add_task_url("https://x/f.zip", "123", "anime")
    api.add_task_bt("ab" * 20, "123")

    assert calls[0]["url"] == "https://clouddownload.115.com/web/"
    assert calls[0]["data"] == {"url": "https://x/f.zip", "wp_path_id": "123",
                                "savepath": "anime", "ac": "add_task_url"}
    # BT 接口不传 savepath，只带 wp_path_id
    assert calls[1]["data"] == {"info_hash": "ab" * 20, "wp_path_id": "123",
                                "ac": "add_task_bt"}
    assert "params" not in calls[0] and "params" not in calls[1]


def test_check_reads_error_msg():
    api = P115Api("missing.json")
    try:
        with pytest.raises(DownloadError) as exc_info:
            api._check({"state": False, "error_msg": "用户任务不存在",
                        "errcode": 10021}, "删除任务")
    finally:
        api._client.close()
    assert exc_info.value.kind == ErrorKind.PERMANENT
    assert "用户任务不存在" in str(exc_info.value)


# ---------------------------------------------------------- exists/cancel

def test_exists_matches_by_hash_or_url():
    dl = make(FakeApi(tasks=[_task()]))
    assert dl.exists(resource("magnet:?xt=urn:btih:" + "AB" * 20, ResourceType.MAGNET))
    dl2 = make(FakeApi(tasks=[_task(info_hash="", url="https://x/f.zip")]))
    assert dl2.exists(resource("https://x/f.zip", ResourceType.HTTP))
    dl3 = make(FakeApi(tasks=[]))
    assert not dl3.exists(resource("magnet:?xt=urn:btih:" + "FF" * 20, ResourceType.MAGNET))


def test_exists_matches_base32_magnet_by_hex_hash():
    b32 = base64.b32encode(bytes.fromhex("ab" * 20)).decode()
    dl = make(FakeApi(tasks=[_task(info_hash="ab" * 20)]))
    assert dl.exists(resource(f"magnet:?xt=urn:btih:{b32}", ResourceType.MAGNET))


def test_cancel_deletes_by_hash():
    api = FakeApi()
    dl = make(api)
    dl.cancel("hash:" + "ab" * 20)
    assert api.deleted == ["ab" * 20]


def test_cancel_without_hash_is_logged_not_silent(caplog):
    api = FakeApi()
    dl = make(api)
    with caplog.at_level(logging.WARNING, logger="omnifetch.downloaders.p115"):
        dl.cancel("url:https://example.com/file.zip")
    assert api.deleted == []
    assert any("info_hash" in record.getMessage() for record in caplog.records)


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


def test_cookie_file_rewrite_clears_expired_flag(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"UID": "u1"}), encoding="utf-8")
    api = P115Api(str(path))
    api.cookie_expired = True
    assert api.refresh_cookies() is False  # 文件没变，保持失效状态
    path.write_text(json.dumps({"UID": "u2"}), encoding="utf-8")
    os.utime(path, (path.stat().st_atime + 10, path.stat().st_mtime + 10))
    assert api.refresh_cookies() is True
    assert api.cookie_expired is False
    assert api._client.cookies.get("UID") == "u2"


def test_redirect_marks_cookie_expired():
    api = P115Api("missing.json")

    class _Response:
        status_code = 302

    class _Client:
        def request(self, method, url, **kw):
            return _Response()

    api._client.close()
    api._client = _Client()
    with pytest.raises(DownloadError) as exc_info:
        api._request("GET", "https://115.com/")
    assert exc_info.value.kind == ErrorKind.PERMANENT
    assert api.cookie_expired is True


def test_expired_cookie_blocks_submit_and_poll_without_network():
    api = FakeApi(tasks=[_task()])
    api.cookie_expired = True
    dl = make(api)
    with pytest.raises(DownloadError) as exc_info:
        dl.submit(resource("magnet:?xt=urn:btih:" + "AA" * 20, ResourceType.MAGNET))
    assert exc_info.value.kind == ErrorKind.PERMANENT
    assert "115-login" in str(exc_info.value)
    result = dl.poll("hash:" + "ab" * 20)
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.TRANSIENT
    assert "115-login" in result.error_message
    ok, detail = dl.ping()
    assert not ok and "115-login" in detail
    assert api.calls == 0  # 失效期间不再打 115 接口
    assert api.added_urls == []


# ------------------------------------------------------- error classification

def test_permanent_keyword_wins_over_transient_hint():
    # "链接错误" is terminal even though the text also says "请稍后".
    error = p115._map_error("链接错误，请稍后重试", "添加离线任务")
    assert error.kind == ErrorKind.PERMANENT

    error = p115._map_error("系统繁忙，请稍后重试", "添加离线任务")
    assert error.kind == ErrorKind.TRANSIENT

    error = p115._map_error("未知问题", "添加离线任务")
    assert error.kind == ErrorKind.PERMANENT


def test_api_client_ignores_proxy_and_can_be_closed(monkeypatch):
    captured: dict = {}

    class RecordingApi:
        def __init__(self, cookie_file, **kw):
            captured["cookie_file"] = cookie_file
            captured["kw"] = kw

    monkeypatch.setattr(p115, "P115Api", RecordingApi)
    dl = P115Downloader("p115", {"cookie_file": "x.json", "proxy": "http://p:8080"})
    dl._get_api()
    assert captured["cookie_file"] == "x.json"
    assert "proxy" not in captured["kw"]  # proxy only applies to .torrent fetches

    api = P115Api("missing.json")
    api.close()
    assert api._client.is_closed


def test_parse_cookie_string_keeps_only_login_cookies():
    jar = p115.parse_cookie_string(
        "UID=u1; CID=c1; SEID=s1; KID=k1; other=leak\nAccepts: text/html")
    assert jar == {"UID": "u1", "CID": "c1", "SEID": "s1", "KID": "k1"}
    with pytest.raises(ValueError):
        p115.parse_cookie_string("foo=bar; baz=qux")


def test_write_cookie_file_is_owner_only(tmp_path):
    path = tmp_path / "nested" / "cookies.json"
    p115.write_cookie_file(path, {"UID": "u", "CID": "c"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"UID": "u", "CID": "c"}
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_downloader_daily_limit_defaults():
    from omnifetch.downloaders import dispatch_daily_limit

    assert dispatch_daily_limit("p115", {}) == 50
    assert dispatch_daily_limit("p115", {"dispatch_daily_limit": ""}) == 50
    assert dispatch_daily_limit("p115", {"dispatch_daily_limit": 0}) == 0
    assert dispatch_daily_limit("aria2", {}) == 0
    assert dispatch_daily_limit("qbittorrent", {"dispatch_daily_limit": 5}) == 5
    assert dispatch_daily_limit("p115", {"dispatch_daily_limit": "7"}) == 7
    assert P115Downloader.DEFAULT_CONFIG["dispatch_daily_limit"] == 50
