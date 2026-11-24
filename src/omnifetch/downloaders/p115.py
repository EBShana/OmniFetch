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
  target_dir_id  离线保存目录 id，默认 "0"（网盘根目录）
  savepath       目录下的相对子路径，默认 ""
  proxy          可选；仅用于抓取 .torrent 文件解析 infohash，115 本身直连
  user_agent     可选

external_id 约定：`hash:<btih>`（磁链/种子）或 `url:<uri>`（http/ftp/ed2k），
任务列表按 info_hash / url 匹配。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
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

_QR_TOKEN = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
_QR_PROMPT = "https://qrcodeapi.115.com/api/2.0/prompt.php"
_QR_LOGIN = "https://qrcodeapi.115.com/app/1.0/web/1.0/login/qrcode/"
_SIGN_URL = "https://115.com/?ct=clouddownload&ac=space"
_CLOUD = "https://clouddownload.115.com/"

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
        self._load_cookies()
        self._sign: tuple[str, str] | None = None
        self._sign_at = 0.0

    def close(self) -> None:
        self._client.close()

    # -- cookies
    def _load_cookies(self) -> None:
        if not self._cookie_file.is_file():
            return
        data = json.loads(self._cookie_file.read_text(encoding="utf-8"))
        for name, value in data.items():
            self._client.cookies.set(name, value, domain=".115.com")

    def save_cookies(self) -> None:
        wanted = {"UID", "CID", "SEID", "KID"}
        jar = {name: value for name, value in self._client.cookies.items()
               if name in wanted}
        if not jar:
            raise DownloadError("登录后未获得有效 Cookie", ErrorKind.PERMANENT)
        self._cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self._cookie_file.write_text(json.dumps(jar, indent=2), encoding="utf-8")

    # -- request core
    def _request(self, method: str, url: str, **kw) -> dict:
        response = self._client.request(method, url, **kw)
        if response.status_code in (301, 302, 303, 307, 308):
            raise DownloadError(
                "115 Cookie 已失效（302 跳登录页），请重新运行 omnifetch 115-login 扫码",
                ErrorKind.PERMANENT,
            )
        try:
            return response.json()
        except ValueError:
            raise DownloadError(
                f"115 返回非 JSON（HTTP {response.status_code}）",
                ErrorKind.TRANSIENT,
            ) from None

    def _check(self, data: dict, action: str) -> dict:
        if not data.get("state"):
            raise _map_error(data.get("error", ""), action)
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
                     savepath: str = "") -> dict:
        sign, ts = self._sign_token()
        data = self._request("POST", _CLOUD, params={"ac": "add_task_url"},
                             data={"url": url, "wp_path_id": wp_path_id,
                                   "savepath": savepath, "sign": sign, "time": ts})
        return self._check(data, "添加离线任务")

    def add_task_bt(self, info_hash: str, wp_path_id: str = "0") -> dict:
        sign, ts = self._sign_token()
        data = self._request("POST", _CLOUD, params={"ac": "add_task_bt"},
                             data={"info_hash": info_hash,
                                   "wp_path_id": wp_path_id,
                                   "sign": sign, "time": ts})
        return self._check(data, "添加离线任务")

    def task_lists(self, page: int = 1, page_size: int = 30, stat: int = 0) -> dict:
        return self._request("GET", _CLOUD, params={
            "ac": "task_lists", "page": page, "page_size": page_size, "stat": stat,
        })

    def task_del(self, info_hash: str) -> None:
        sign, ts = self._sign_token()
        try:
            self._check(self._request("POST", _CLOUD, params={"ac": "del_task"},
                                      data={"hash": info_hash,
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
                      "target_dir_id": "0", "savepath": "", "proxy": ""}

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._cookie_file = config.get("cookie_file") or ""
        self._target_dir = str(config.get("target_dir_id") or "0")
        self._savepath = config.get("savepath") or ""
        self._proxy = config.get("proxy") or None
        self._api: P115Api | None = None

    def _get_api(self) -> P115Api:
        if self._api is None:
            if not self._cookie_file:
                raise DownloadError(
                    "未配置 cookie_file，请先运行 omnifetch 115-login "
                    f"{self.name} 扫码登录", ErrorKind.PERMANENT,
                )
            self._api = P115Api(self._cookie_file)
        return self._api

    # -------------------------------------------------------------- submit

    def submit(self, resource: Resource) -> str:
        uri = next((u for u in resource.download_uris if u), "")
        if not uri:
            raise DownloadError("资源没有下载地址", ErrorKind.PERMANENT)
        api = self._get_api()
        magnet = _BTITH_RE.search(uri)
        try:
            if magnet:
                api.add_task_url(uri, self._target_dir, self._savepath)
                return f"hash:{btih_hex(magnet.group(1))}"
            if resource.resource_type == ResourceType.TORRENT or \
                    uri.lower().endswith(".torrent"):
                # .torrent 文件：解析 infohash 后走 bt 接口（无需上传种子）
                digest = torrent_infohash(self._fetch_torrent(uri))
                api.add_task_bt(digest, self._target_dir)
                return f"hash:{digest}"
            api.add_task_url(uri, self._target_dir, self._savepath)
            return f"url:{uri}"
        except DownloadError as exc:
            # 任务已存在 = 幂等成功：返回既有任务的 external_id
            if "已存在" in str(exc):
                existing = self._find_task(uri)
                if existing is not None:
                    return self._task_key(existing, fallback_uri=uri)
            raise

    def _fetch_torrent(self, url: str) -> bytes:
        try:
            resp = httpx.get(url, proxy=self._proxy, timeout=60,
                             follow_redirects=True)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            raise DownloadError(f"抓取种子文件失败：{exc}", ErrorKind.TRANSIENT) from exc

    # --------------------------------------------------------------- query

    def _iter_tasks(self, max_pages: int = 5):
        api = self._get_api()
        for page in range(1, max_pages + 1):
            data = api.task_lists(page=page, stat=0)
            tasks = data.get("tasks") or []
            yield from tasks
            if page >= int(data.get("page_count") or 1) or not tasks:
                break

    def _task_key(self, task: dict, fallback_uri: str = "") -> str:
        info_hash = (task.get("info_hash") or "").lower()
        if info_hash:
            return f"hash:{info_hash}"
        return f"url:{task.get('url') or fallback_uri}"

    def _find_task(self, uri_or_key: str) -> dict | None:
        needle = uri_or_key.lower()
        magnet = _BTITH_RE.search(needle)
        want_hash = (needle[5:] if needle.startswith("hash:")
                     else btih_hex(magnet.group(1)) if magnet else "")
        for task in self._iter_tasks():
            if want_hash and (task.get("info_hash") or "").lower() == want_hash:
                return task
            if (task.get("url") or "").lower() == needle:
                return task
            if self._task_key(task).lower() == needle:
                return task
        return None

    # ------------------------------------------------------------ contract

    def poll(self, external_id: str) -> TaskResult:
        task = self._find_task(external_id)
        if task is None:
            return TaskResult(PollStatus.FAILED, error_kind=ErrorKind.PERMANENT,
                              error_message="115 离线任务不存在（可能已过期或被删除）")
        fail_msg = task.get("fail_msg") or ""
        actions = task.get("available_actions") or []
        if fail_msg or "retry" in actions:
            return TaskResult(PollStatus.FAILED, error_kind=ErrorKind.PERMANENT,
                              error_message=f"115 离线任务失败：{fail_msg or '标记为可重试'}")
        percent = float(task.get("percent") or 0)
        if int(task.get("status") or 0) == 1 or percent >= 100:
            return TaskResult(PollStatus.DONE, progress=1.0)
        return TaskResult(PollStatus.PROGRESS, progress=min(percent, 99.9) / 100)

    def exists(self, resource: Resource) -> bool:
        uri = next((u for u in resource.download_uris if u), "")
        if not uri:
            return False
        try:
            return self._find_task(uri) is not None
        except DownloadError:
            return False

    def cancel(self, external_id: str) -> None:
        info_hash = external_id.partition(":")[2] if external_id.startswith("hash:") else ""
        if info_hash:
            self._get_api().task_del(info_hash)

    def ping(self) -> tuple[bool, str]:
        try:
            data = self._get_api().task_lists(page=1, page_size=1)
            return True, f"115 在线，离线任务共 {data.get('count', '?')} 个"
        except DownloadError as exc:
            return False, str(exc)
