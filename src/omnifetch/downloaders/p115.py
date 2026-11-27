"""115 网盘离线下载插件 —— 手写最小 Web API 客户端。

为什么不用 p115client：它依赖 ~40 个包，其中 zstandard 在 Python 3.15
上没有预编译 wheel 且本机无 C 编译器，装不上；对我们的需求（添加/查询
离线任务）也太重。接口语义参考 p115client 源码（0.0.9.7.8.3）并实测确认。

风控原则（重要）：
  - 复用持久化 Cookie，绝不自动重新登录——登录是最触发风控的动作；
    Cookie 失效（302）时报错并提示手动扫码
  - 所有请求串行发出，不并发轰炸
  - 桌面浏览器 UA，sign 令牌缓存 10 分钟（获取 sign 也算一次请求）

Config keys (downloaders.d/*.toml):
  cookie_file    扫码登录产物（JSON），由 `omnifetch 115-login <实例名>` 生成
  target_dir_id  离线保存目录 id，默认 "0"（网盘根目录）；BT/磁链会在此目录下
                 由 115 自动按种子名建子目录
  savepath       仅对普通链接（http/ed2k）任务生效的保存子路径，默认 ""
                 （BT/磁链不传 savepath，避免覆盖 115 的自动建目录行为）
  proxy          可选；仅用于抓取 .torrent 文件解析 infohash，115 本身直连
  submit_grace_seconds  新任务在列表里出现前的宽限期，默认 90 秒
  snapshot_ttl   任务列表快照的复用时长（秒），默认 10
  user_agent     可选（当前未生效，见开发清单）

任务失败时，若 115 的 retry_count 还没到 retry_limit，先按 hash 删掉这条
失败任务，再以可重试错误交给核心重新提交，避免离线列表堆积；达到上限才
返回永久失败。

external_id 约定：`hash:<btih>`（磁链/种子，或 115 添加任务时返回了 info_hash
的链接）或 `url:<uri>`（115 没返回 hash 的 http/ftp/ed2k），任务列表按
info_hash / url 匹配。cancel 只能按 hash 删除，url 键的任务会被明确记日志忽略。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

from ..core.models import ErrorKind, Resource, ResourceType
from . import register
from .base import DownloaderPlugin, DownloadError, PollStatus, TaskResult

log = logging.getLogger(__name__)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_BTITH_RE = re.compile(r"btih:([0-9a-zA-Z]{32,40})")
# 安全上限：防 page_count 异常时无限翻页；触发后明确标记结果不完整。
_MAX_SCAN_PAGES = 100

_QR_TOKEN = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
_QR_PROMPT = "https://qrcodeapi.115.com/api/2.0/prompt.php"
_QR_LOGIN = "https://qrcodeapi.115.com/app/1.0/web/1.0/login/qrcode/"
_SIGN_URL = "https://115.com/?ct=clouddownload&ac=space"
_CLOUD = "https://clouddownload.115.com/"
# 添加任务只接受 /web/ 路径且 ac 放在表单；根路径会返回“请升级新版本”。
_CLOUD_WEB = "https://clouddownload.115.com/web/"

_PERMANENT_ERROR_KEYWORDS = (
    "已存在", "登录", "cookie", "无效的链接", "不支持的链接", "链接错误", "不存在",
)
_TRANSIENT_ERROR_KEYWORDS = (
    "空间不足", "配额", "上限", "频繁", "稍后", "系统繁忙", "网络", "超时", "风控",
)


def _map_error(message: str, action: str) -> DownloadError:
    text = message or f"{action} 失败"
    # Terminal causes win even when the text also contains retry hints
    # (e.g. "链接错误，请稍后重试" must not be retried forever).
    if any(k in text for k in _PERMANENT_ERROR_KEYWORDS):
        return DownloadError(f"115 {action}: {text}", ErrorKind.PERMANENT)
    if any(k in text for k in _TRANSIENT_ERROR_KEYWORDS):
        return DownloadError(f"115 {action}: {text}", ErrorKind.TRANSIENT)
    return DownloadError(f"115 {action}: {text}", ErrorKind.PERMANENT)


def btih_hex(token: str) -> str:
    """Normalize a magnet BTIH to lowercase hex.

    Links carry the hash either as 40-char hex or as 32-char Base32
    (RFC 3986); 115 compares it as hex, so Base32 is decoded first.
    """
    token = (token or "").strip()
    if len(token) == 40:
        return token.lower()
    try:
        raw = base64.b32decode(token.upper())
    except (binascii.Error, ValueError):
        return token.lower()
    return raw.hex() if len(raw) == 20 else token.lower()


_COOKIE_NAMES = ("UID", "CID", "SEID", "KID")


def parse_cookie_string(text: str) -> dict[str, str]:
    """Parse a pasted Cookie header ("UID=…; CID=…") into the 115 jar.

    Only the four login cookies are kept: browser headers often carry
    unrelated cookies that must not be persisted or sent to 115.
    """
    jar: dict[str, str] = {}
    for part in (text or "").replace("\n", ";").split(";"):
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if sep and name in _COOKIE_NAMES and value:
            jar[name] = value
    if not jar:
        raise ValueError("未找到 UID/CID/SEID/KID，请粘贴已登录 115 的 Cookie")
    return jar


def write_cookie_file(cookie_file: str | Path, jar: dict[str, str]) -> None:
    """Write the cookie jar JSON and restrict it to the owner (0600)."""
    path = Path(cookie_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jar, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows does not always honor POSIX mode bits
        pass



# --------------------------------------------------------------------- bencode

def torrent_infohash(content: bytes) -> str:
    """SHA1 of the bencoded `info` dict = torrent infohash (hex)."""
    def parse(pos: int):
        ch = content[pos:pos + 1]
        if ch == b"i":
            end = content.index(b"e", pos)
            return int(content[pos + 1:end]), end + 1
        if ch == b"l":
            pos += 1
            out = []
            while content[pos:pos + 1] != b"e":
                value, pos = parse(pos)
                out.append(value)
            return out, pos + 1
        if ch == b"d":
            pos += 1
            out = {}
            while content[pos:pos + 1] != b"e":
                key, pos = parse(pos)
                out[key], pos = parse(pos)
            return out, pos + 1
        if ch.isdigit():
            colon = content.index(b":", pos)
            length = int(content[pos:colon])
            start = colon + 1
            return content[start:start + length], start + length
        raise ValueError(f"bad bencode at {pos}")

    _, end = parse(0)
    # re-walk the top dict, tracking the raw span of the info value
    pos = 1  # skip leading 'd'
    while content[pos:pos + 1] != b"e":
        key, pos = parse(pos)
        value_start = pos
        _, pos = parse(pos)
        if key == b"info":
            return hashlib.sha1(content[value_start:pos]).hexdigest()
    raise ValueError("torrent has no info dict")


# ---------------------------------------------------------------------- API

class P115Api:
    """Cookie-auth'd minimal client. follow_redirects=False so a 302 (cookie
    expired) surfaces as a clear error instead of silently hitting the login
    page HTML.

    115 itself is always reached directly: the optional proxy in the config
    only applies to fetching .torrent files (see P115Downloader)."""

    def __init__(self, cookie_file: str, user_agent: str = DEFAULT_UA):
        self._cookie_file = Path(cookie_file)
        self._client = httpx.Client(timeout=30, follow_redirects=False)
        self._client.headers["User-Agent"] = user_agent
        self._cookies_mtime: float | None = None
        self.cookie_expired = False
        self._load_cookies()
        self._sign: tuple[str, str] | None = None
        self._sign_at = 0.0

    def close(self) -> None:
        self._client.close()

    # -- cookies
    def _load_cookies(self) -> None:
        try:
            mtime = self._cookie_file.stat().st_mtime
        except OSError:
            self._cookies_mtime = None
            return
        data = json.loads(self._cookie_file.read_text(encoding="utf-8"))
        for name, value in data.items():
            self._client.cookies.set(name, value, domain=".115.com")
        self._cookies_mtime = mtime

    def refresh_cookies(self) -> bool:
        """Cookie 文件被重新扫码覆盖时热加载，避免必须重启进程。"""
        try:
            mtime = self._cookie_file.stat().st_mtime
        except OSError:
            return False
        if self._cookies_mtime is not None and mtime == self._cookies_mtime:
            return False
        self._client.cookies.clear()
        self._load_cookies()
        self.cookie_expired = False
        return True

    def save_cookies(self) -> None:
        wanted = {"UID", "CID", "SEID", "KID"}
        jar = {name: value for name, value in self._client.cookies.items()
               if name in wanted}
        if not jar:
            raise DownloadError("登录后未获得有效 Cookie", ErrorKind.PERMANENT)
        write_cookie_file(self._cookie_file, jar)

    # -- request core
    def _request(self, method: str, url: str, **kw) -> dict:
        response = self._client.request(method, url, **kw)
        if response.status_code in (301, 302, 303, 307, 308):
            self.cookie_expired = True
            raise DownloadError(
                "115 Cookie 已失效（302 跳登录页），请重新运行 omnifetch 115-login 扫码",
                ErrorKind.PERMANENT,
            )
        try:
            data = response.json()
        except ValueError:
            raise DownloadError(
                f"115 返回非 JSON（HTTP {response.status_code}）",
                ErrorKind.TRANSIENT,
            ) from None
        self.cookie_expired = False
        return data

    def _check(self, data: dict, action: str) -> dict:
        if not data.get("state"):
            raise _map_error(data.get("error_msg") or data.get("error", ""), action)
        return data

    # -- sign（添加任务类接口需要；缓存 10 分钟）
    def _sign_token(self) -> tuple[str, str]:
        if self._sign and time.time() - self._sign_at < 600:
            return self._sign
        data = self._check(self._request("GET", _SIGN_URL), "获取 sign")
        self._sign = (str(data["sign"]), str(data["time"]))
        self._sign_at = time.time()
        return self._sign

    # -- offline operations
    def add_task_url(self, url: str, wp_path_id: str = "0",
                     savepath: str | None = None) -> dict:
        data = {"url": url, "wp_path_id": wp_path_id, "ac": "add_task_url"}
        if savepath is not None:
            data["savepath"] = savepath
        return self._check(
            self._request("POST", _CLOUD_WEB, data=data), "添加离线任务")

    def add_task_bt(self, info_hash: str, wp_path_id: str = "0",
                    savepath: str | None = None) -> dict:
        data = {"info_hash": info_hash, "wp_path_id": wp_path_id,
                "ac": "add_task_bt"}
        if savepath is not None:
            data["savepath"] = savepath
        return self._check(
            self._request("POST", _CLOUD_WEB, data=data), "添加离线任务")

    def task_lists(self, page: int = 1, page_size: int = 30, stat: int = 0) -> dict:
        return self._request("GET", _CLOUD, params={
            "ac": "task_lists", "page": page, "page_size": page_size, "stat": stat,
        })

    def task_del(self, info_hash: str) -> None:
        sign, ts = self._sign_token()
        try:
            self._check(self._request("POST", _CLOUD, params={"ac": "task_del"},
                                      data={"hash[0]": info_hash,
                                            "sign": sign, "time": ts}), "删除任务")
        except DownloadError as exc:
            log.info("115 delete task best-effort failed: %s", exc)


def qrcode_login(cookie_file: str, user_agent: str = DEFAULT_UA,
                 timeout: int = 180) -> None:
    """扫码登录：终端显示二维码，115 App 扫描确认后 Cookie 写入文件。"""
    import qrcode

    client = httpx.Client(timeout=30)
    client.headers["User-Agent"] = user_agent
    try:
        token = client.get(_QR_TOKEN).json()["data"]
        qr = qrcode.QRCode(border=1)
        qr.add_data(token["qrcode"])
        qr.print_ascii(invert=True)
        print("请用 115 手机 App 扫描上方二维码（%d 秒内）…" % timeout)

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            resp = client.get(_QR_PROMPT, params={
                "uid": token["uid"], "sign": token["sign"],
                "time": token["time"], "_": int(time.time() * 1000),
            }).json()
            status = (resp.get("data") or {}).get("status")
            if status == 2:
                break
            if status is False or (not resp.get("state") and status is None):
                raise DownloadError(f"扫码状态异常：{resp.get('message')}",
                                    ErrorKind.PERMANENT)
        else:
            raise DownloadError("扫码超时，请重试", ErrorKind.TRANSIENT)

        login = client.post(_QR_LOGIN, data={"account": token["uid"]})
        if login.status_code >= 400:
            raise DownloadError(f"登录确认失败 HTTP {login.status_code}",
                                ErrorKind.TRANSIENT)

        # 汇总登录 Cookie 到专用客户端，验证登录态后落盘
        api = P115Api(cookie_file=cookie_file)
        try:
            for morsel in client.cookies.jar:
                api._client.cookies.set(morsel.name, morsel.value, domain=morsel.domain)
            api._check(api._request("GET", _SIGN_URL), "验证登录")
            api.save_cookies()
        finally:
            api.close()
    finally:
        client.close()
    print(f"登录成功，Cookie 已保存到 {cookie_file}")


# -------------------------------------------------------------------- plugin

@register
class P115Downloader(DownloaderPlugin):
    type = "p115"
    accepts = (ResourceType.MAGNET, ResourceType.TORRENT, ResourceType.HTTP,
               ResourceType.NETDISK)
    DEFAULT_CONFIG = {"cookie_file": "data/p115_cookies.json",
                      "target_dir_id": "3571296246695987055",
                      "savepath": "", "proxy": "",
                      "submit_grace_seconds": 90, "snapshot_ttl": 10,
                      "dispatch_daily_limit": 50}

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._cookie_file = config.get("cookie_file") or ""
        self._target_dir = str(config.get("target_dir_id") or "0")
        self._savepath = config.get("savepath") or ""
        self._proxy = config.get("proxy") or None
        self._api: P115Api | None = None
        self._grace = float(config.get("submit_grace_seconds", 90) or 0)
        self._submitted: dict[str, float] = {}  # external_id -> 提交时刻
        self._max_scan_pages = _MAX_SCAN_PAGES
        self._snapshot_tasks: list[dict] | None = None
        self._snapshot_complete = True
        self._snapshot_at = 0.0
        self._now = time.monotonic

    def _get_api(self) -> P115Api:
        if self._api is None:
            if not self._cookie_file:
                raise DownloadError(
                    "未配置 cookie_file，请先运行 omnifetch 115-login "
                    f"{self.name} 扫码登录", ErrorKind.PERMANENT,
                )
            self._api = P115Api(self._cookie_file)
        return self._api

    def _cookie_hint(self) -> str:
        return (f"115 Cookie 已失效，请重新运行 omnifetch 115-login {self.name} "
                "扫码后再试")

    def _cookie_expired_result(self) -> TaskResult:
        # 云端任务可能还在跑，判永久失败会让核心换源重下；标成可重试，
        # 重新扫码后 submit 走“已存在”分支即可恢复跟踪。
        return TaskResult(PollStatus.FAILED, error_kind=ErrorKind.TRANSIENT,
                          error_message=self._cookie_hint())

    # -------------------------------------------------------------- submit

    def submit(self, resource: Resource) -> str:
        uri = next((u for u in resource.download_uris if u), "")
        if not uri:
            raise DownloadError("资源没有下载地址", ErrorKind.PERMANENT)
        api = self._get_api()
        api.refresh_cookies()
        if api.cookie_expired:
            # 别再拿失效 Cookie 去撞 115 接口
            raise DownloadError(self._cookie_hint(), ErrorKind.PERMANENT)
        magnet = _BTITH_RE.search(uri)
        try:
            if magnet:
                returned = self._returned_hash(
                    api.add_task_url(uri, self._target_dir))
                key = f"hash:{returned or btih_hex(magnet.group(1))}"
            elif resource.resource_type == ResourceType.TORRENT or \
                    uri.lower().endswith(".torrent"):
                # .torrent 文件：解析 infohash 后走 bt 接口（无需上传种子）
                digest = torrent_infohash(self._fetch_torrent(uri))
                returned = self._returned_hash(
                    api.add_task_bt(digest, self._target_dir))
                key = f"hash:{returned or digest}"
            else:
                returned = self._returned_hash(
                    api.add_task_url(uri, self._target_dir, self._savepath))
                # 115 对部分链接会返回 info_hash；拿到了才能按 hash 取消
                key = f"hash:{returned}" if returned else f"url:{uri}"
        except DownloadError as exc:
            # 任务已存在 = 幂等成功：返回既有任务的 external_id
            if "已存在" not in str(exc):
                raise
            existing = self._find_task(uri, force=True)[0]
            if existing is None:
                raise
            key = self._task_key(existing, fallback_uri=uri)
        self._remember_submitted(key)
        return key

    @staticmethod
    def _returned_hash(data: dict) -> str:
        """取 115 添加任务响应里的 info_hash；没有则返回空串。"""
        raw = str((data or {}).get("info_hash") or "").strip()
        return btih_hex(raw) if raw else ""

    def _remember_submitted(self, key: str) -> None:
        """记录提交时刻：115 列表入库有延迟，宽限期内 poll 不判失败。"""
        now = self._now()
        self._submitted[key] = now
        cutoff = now - max(self._grace * 4, 600)
        for stale in [k for k, ts in self._submitted.items() if ts < cutoff]:
            del self._submitted[stale]
        # 新任务还不在这份快照里，强制下一轮重新扫描
        self._snapshot_tasks = None

    def _in_submit_grace(self, key: str) -> bool:
        submitted_at = self._submitted.get(key)
        return submitted_at is not None and self._now() - submitted_at <= self._grace

    # --------------------------------------------------------------- retry

    def _delete_failed_task(self, external_id: str, task: dict) -> None:
        """重提交前清掉云端失败任务，避免列表堆积和“已存在”撞车。"""
        info_hash = (task.get("info_hash") or "").lower()
        if not info_hash and external_id.startswith("hash:"):
            info_hash = external_id.partition(":")[2]
        if not info_hash:
            log.warning("115 失败任务 %s 没有 info_hash，无法清理，直接重试",
                        external_id)
            return
        self._get_api().task_del(info_hash)

    def _fetch_torrent(self, url: str) -> bytes:
        try:
            resp = httpx.get(url, proxy=self._proxy, timeout=60,
                             follow_redirects=True)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            raise DownloadError(f"抓取种子文件失败：{exc}", ErrorKind.TRANSIENT) from exc

    # --------------------------------------------------------------- query

    def _scan_all_tasks(self) -> tuple[list[dict], bool]:
        """翻完所有分页；触到安全上限时明确返回“不完整”，绝不静默截断。"""
        api = self._get_api()
        found: list[dict] = []
        page = 1
        while True:
            data = api.task_lists(page=page, stat=0)
            tasks = data.get("tasks") or []
            found.extend(tasks)
            if not tasks or page >= int(data.get("page_count") or 1):
                return found, True
            page += 1
            if page > self._max_scan_pages:
                log.warning(
                    "115 任务列表超过扫描上限 %d 页，本次结果不完整",
                    self._max_scan_pages,
                )
                return found, False

    def _tasks_snapshot(self, force: bool = False) -> tuple[list[dict], bool]:
        """一个 TTL 内复用同一份任务列表，避免每个任务每轮都全量翻页。"""
        ttl = float(self.config.get("snapshot_ttl", 10) or 0)
        if (not force and self._snapshot_tasks is not None
                and self._now() - self._snapshot_at < ttl):
            return self._snapshot_tasks, self._snapshot_complete
        tasks, complete = self._scan_all_tasks()
        self._snapshot_tasks = tasks
        self._snapshot_complete = complete
        self._snapshot_at = self._now()
        return tasks, complete

    def _task_key(self, task: dict, fallback_uri: str = "") -> str:
        info_hash = (task.get("info_hash") or "").lower()
        if info_hash:
            return f"hash:{info_hash}"
        return f"url:{task.get('url') or fallback_uri}"

    def _find_task(self, uri_or_key: str, force: bool = False) -> tuple[dict | None, bool]:
        """按 external_id 找任务；第二个返回值表示列表是否扫全。"""
        needle = uri_or_key.lower()
        magnet = _BTITH_RE.search(needle)
        want_hash = (needle[5:] if needle.startswith("hash:")
                     else btih_hex(magnet.group(1)) if magnet else "")
        tasks, complete = self._tasks_snapshot(force=force)
        for task in tasks:
            if want_hash and (task.get("info_hash") or "").lower() == want_hash:
                return task, complete
            if (task.get("url") or "").lower() == needle:
                return task, complete
            if self._task_key(task).lower() == needle:
                return task, complete
        return None, complete

    # ------------------------------------------------------------ contract

    def poll(self, external_id: str) -> TaskResult:
        api = self._get_api()
        api.refresh_cookies()
        if api.cookie_expired:
            return self._cookie_expired_result()
        try:
            task, complete = self._find_task(external_id)
        except DownloadError:
            if api.cookie_expired:
                return self._cookie_expired_result()
            raise
        if task is None:
            if not complete:
                # 列表没扫全时“找不到”不可信，宁可等下一轮也不要误杀任务
                log.warning("115 任务列表未扫全，暂不判定任务 %s 失败", external_id)
                return TaskResult(PollStatus.PROGRESS, queued=True)
            if self._in_submit_grace(external_id):
                # 刚提交的任务可能还没出现在 115 的任务列表里
                return TaskResult(PollStatus.PROGRESS, queued=True)
            return TaskResult(PollStatus.FAILED, error_kind=ErrorKind.PERMANENT,
                              error_message="115 离线任务不存在（可能已过期或被删除）")
        self._submitted.pop(external_id, None)
        # 真机失败样本：status=-1、display_status=failed、status_text=下载失败。
        failed = (str(task.get("display_status") or "") == "failed"
                  or int(task.get("status") or 0) == -1
                  or "失败" in str(task.get("status_text") or ""))
        if failed:
            message = f"115 离线任务失败：{task.get('status_text') or '未知原因'}"
            retry_count = int(task.get("retry_count") or 0)
            retry_limit = int(task.get("retry_limit") or 0)
            if retry_count < retry_limit:
                # 先删掉云端失败任务，再交给核心重新提交
                self._delete_failed_task(external_id, task)
                return TaskResult(PollStatus.FAILED, error_kind=ErrorKind.TRANSIENT,
                                  error_message=message)
            return TaskResult(PollStatus.FAILED, error_kind=ErrorKind.PERMANENT,
                              error_message=message)
        percent = float(task.get("percentDone") or 0)
        if (str(task.get("display_status") or "") == "finished"
                or int(task.get("status") or 0) == 2 or percent >= 100):
            return TaskResult(PollStatus.DONE, progress=1.0)
        # percentDone 为 0 = 云端还没开始拉取（排队），这期间不计停滞
        return TaskResult(PollStatus.PROGRESS, progress=min(percent, 99.9) / 100,
                          queued=percent <= 0)

    def exists(self, resource: Resource) -> bool:
        uri = next((u for u in resource.download_uris if u), "")
        if not uri:
            return False
        try:
            return self._find_task(uri)[0] is not None
        except DownloadError:
            return False

    def cancel(self, external_id: str) -> None:
        info_hash = external_id.partition(":")[2] if external_id.startswith("hash:") else ""
        if not info_hash:
            # 没有 hash 就没法按 115 的 task_del 删除；明确记一笔而不是静默跳过
            log.warning(
                "115 任务 %s 没有 info_hash，无法在 115 侧取消"
                "（已忽略，云端任务可能继续运行）", external_id)
            return
        self._get_api().task_del(info_hash)

    def ping(self) -> tuple[bool, str]:
        try:
            api = self._get_api()
            api.refresh_cookies()
            if api.cookie_expired:
                return False, self._cookie_hint()
            tasks, complete = self._tasks_snapshot()
        except DownloadError as exc:
            return False, str(exc)
        suffix = "" if complete else "（列表未扫完）"
        return True, f"115 在线，离线任务共 {len(tasks)} 个{suffix}"
