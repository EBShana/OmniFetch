"""aria2 downloader via JSON-RPC.

Config keys:
  rpc_url       e.g. http://aria2:6800/jsonrpc (required)
  rpc_secret    aria2 rpc-secret token (optional)
  download_dir  destination directory in aria2's filesystem (optional,
                passed as the ``dir`` option of addUri)

Status mapping (tellStatus):
  active/waiting/paused  -> PROGRESS (completedLength/totalLength)
  complete               -> DONE
  removed                -> FAILED permanent
  error                  -> FAILED; errorCode decides transient/permanent
  "GID ... not found"    -> FAILED permanent (job lost, e.g. aria2 restarted)

Magnets and .torrent URLs: aria2 first downloads the torrent file/magnet
metadata, then spawns the real download as a ``followedBy`` job. poll()
follows that chain (up to 5 hops) and reports the new gid via
TaskResult.external_id so the core retargets the task.

Error codes treated as permanent (dead source — retrying is pointless):
  3  resource not found      4  max-file-not-found
  12 file already exists
Everything else (timeouts, network, disk full, ...) is transient.
"""
from __future__ import annotations

import itertools

import httpx

from ..core.models import ErrorKind, Resource, ResourceType
from . import register
from .base import DownloaderPlugin, DownloadError, PollStatus, TaskResult

_TELL_KEYS = [
    "status", "completedLength", "totalLength",
    "errorCode", "errorMessage", "followedBy",
]
_PERMANENT_ERROR_CODES = {3, 4, 12}
_MAX_FOLLOW_HOPS = 5


class Aria2Rpc:
    """Minimal typed-ish JSON-RPC client. Transport problems and RPC faults
    raise DownloadError; the kind tells the core whether a retry makes sense."""

    def __init__(self, rpc_url: str, secret: str = "", timeout: float = 15.0):
        self._url = rpc_url
        self._token = f"token:{secret}" if secret else ""
        self._client = httpx.Client(timeout=timeout)
        self._ids = itertools.count(1)

    def call(self, method: str, *params):
        payload = {
            "jsonrpc": "2.0",
            "id": f"omnifetch-{next(self._ids)}",
            "method": f"aria2.{method}",
            "params": ([self._token] if self._token else []) + list(params),
        }
        try:
            response = self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise DownloadError(f"aria2 unreachable: {exc}", ErrorKind.TRANSIENT) from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        # aria2 reports RPC method faults (e.g. "GID ... is not found") with
        # HTTP 400 *and* a JSON-RPC error body, so parse the body first.
        if isinstance(data, dict) and "error" in data:
            code = data["error"].get("code", 0)
            message = data["error"].get("message", "unknown error")
            if code == -32602:  # invalid params — our request is wrong forever
                raise DownloadError(
                    f"aria2 rejected params: {message}", ErrorKind.PERMANENT
                )
            raise DownloadError(f"aria2 rpc error {code}: {message}", ErrorKind.TRANSIENT)
        if response.status_code >= 400:
            raise DownloadError(
                f"aria2 rpc http {response.status_code}", ErrorKind.TRANSIENT
            )
        if not isinstance(data, dict) or "result" not in data:
            raise DownloadError("aria2 rpc malformed response", ErrorKind.TRANSIENT)
        return data["result"]


@register
class Aria2Downloader(DownloaderPlugin):
    type = "aria2"
    accepts = (ResourceType.HTTP, ResourceType.TORRENT, ResourceType.MAGNET)
    DEFAULT_CONFIG = {"rpc_url": "http://127.0.0.1:6800/jsonrpc",
                      "rpc_secret": "", "download_dir": ""}

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._dir = config.get("download_dir") or None
        self._rpc = Aria2Rpc(
            config["rpc_url"], config.get("rpc_secret", "")
        )

    # -------------------------------------------------------------- submit

    def submit(self, resource: Resource) -> str:
        uris = [u for u in resource.download_uris if u]
        if not uris:
            raise DownloadError("resource has no download uris", ErrorKind.PERMANENT)
        options: dict = {}
        if self._dir:
            options["dir"] = self._dir
        try:
            return self._rpc.call("addUri", uris, options)
        except DownloadError as exc:
            # A rejected URI (404-ish at add time) is not going to heal by
            # retrying through the same aria2.
            if "invalid" in str(exc).lower() and "uri" in str(exc).lower():
                raise DownloadError(str(exc), ErrorKind.PERMANENT) from exc
            raise

    # ---------------------------------------------------------------- poll

    def poll(self, external_id: str) -> TaskResult:
        gid = external_id
        status: dict = {}
        for _ in range(_MAX_FOLLOW_HOPS + 1):
            try:
                status = self._rpc.call("tellStatus", gid, _TELL_KEYS)
            except DownloadError as exc:
                if "not found" in str(exc).lower():
                    return TaskResult(
                        PollStatus.FAILED, error_kind=ErrorKind.PERMANENT,
                        error_message=str(exc), external_id=gid,
                    )
                raise
            followed = status.get("followedBy") or []
            if not followed:
                break
            gid = followed[0]  # magnet/.torrent metadata job -> real download

        state = status.get("status")
        if state == "complete":
            return TaskResult(PollStatus.DONE, progress=1.0, external_id=gid)
        if state == "removed":
            return TaskResult(
                PollStatus.FAILED, error_kind=ErrorKind.PERMANENT,
                error_message="download removed from aria2", external_id=gid,
            )
        if state == "error":
            code = int(status.get("errorCode") or 1)
            kind = (
                ErrorKind.PERMANENT
                if code in _PERMANENT_ERROR_CODES
                else ErrorKind.TRANSIENT
            )
            return TaskResult(
                PollStatus.FAILED, error_kind=kind,
                error_message=f"aria2 error {code}: {status.get('errorMessage')}",
                external_id=gid,
            )

        # active / waiting / paused
        total = int(status.get("totalLength") or 0)
        done = int(status.get("completedLength") or 0)
        return TaskResult(
            PollStatus.PROGRESS,
            progress=(done / total) if total else 0.0,
            external_id=gid,
        )

    # -------------------------------------------------------------- cancel

    def cancel(self, external_id: str) -> None:
        for method in ("remove", "removeDownloadResult"):
            try:
                self._rpc.call(method, external_id)
            except DownloadError:
                pass  # already gone — that is the outcome we wanted

    def ping(self) -> tuple[bool, str]:
        try:
            version = self._rpc.call("getVersion")
            return True, f"aria2 {version['version']}"
        except Exception as exc:
            return False, str(exc)
