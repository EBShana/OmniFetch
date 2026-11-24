"""Aria2 downloader unit tests with a stubbed RPC client."""
from __future__ import annotations

import pytest

from omnifetch.core.models import ErrorKind, Resource, ResourceType
from omnifetch.downloaders.base import DownloadError, PollStatus
from omnifetch.downloaders.http import Aria2Downloader


class StubRpc:
    def __init__(self, *queued):
        self.calls: list[tuple] = []
        self.queue = list(queued)

    def call(self, method: str, *params):
        self.calls.append((method, params))
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None


def make(config: dict | None = None, responses: list | None = None) -> Aria2Downloader:
    dl = Aria2Downloader("aria2", {"rpc_url": "http://x", **(config or {})})
    dl._rpc = StubRpc(*(responses or []))
    return dl


def resource(url: str = "https://a.example/f.bin") -> Resource:
    return Resource(
        title="f.bin", source_url=url, source="t",
        download_uris=[url], resource_type=ResourceType.HTTP,
    )


# ------------------------------------------------------------------ submit

def test_submit_adduri_passes_uris_and_dir():
    dl = make({"download_dir": "/downloads"})
    dl._rpc.queue.append("gid-1")
    gid = dl.submit(resource())
    assert gid == "gid-1"
    method, params = dl._rpc.calls[0]
    assert method == "addUri"
    uris, options = params
    assert uris == ["https://a.example/f.bin"]
    assert options["dir"] == "/downloads"


def test_submit_without_uris_is_permanent():
    dl = make()
    bad = resource()
    bad.download_uris = []
    with pytest.raises(DownloadError) as exc_info:
        dl.submit(bad)
    assert exc_info.value.kind == ErrorKind.PERMANENT


# ------------------------------------------------------------------- poll

def test_poll_active_reports_progress_ratio():
    dl = make(responses=[{"status": "active", "completedLength": "512",
                          "totalLength": "1024", "followedBy": []}])
    result = dl.poll("gid-1")
    assert result.status == PollStatus.PROGRESS
    assert result.progress == 0.5


def test_poll_complete_is_done():
    dl = make(responses=[{"status": "complete", "completedLength": "1024",
                          "totalLength": "1024", "followedBy": []}])
    result = dl.poll("gid-1")
    assert result.status == PollStatus.DONE
    assert result.progress == 1.0


def test_poll_error_code_3_is_permanent():
    dl = make(responses=[{"status": "error", "errorCode": "3",
                          "errorMessage": "resource not found", "followedBy": []}])
    result = dl.poll("gid-1")
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT
    assert "error 3" in result.error_message


def test_poll_network_error_is_transient():
    dl = make(responses=[{"status": "error", "errorCode": "6",
                          "errorMessage": "timeout", "followedBy": []}])
    result = dl.poll("gid-1")
    assert result.error_kind == ErrorKind.TRANSIENT


def test_poll_removed_is_permanent():
    dl = make(responses=[{"status": "removed", "followedBy": []}])
    result = dl.poll("gid-1")
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT


def test_poll_follows_magnet_chain_and_reports_new_gid():
    dl = make(responses=[
        # magnet metadata fetch finished -> real download spawned
        {"status": "complete", "completedLength": "0", "totalLength": "0",
         "followedBy": ["gid-2"]},
        {"status": "active", "completedLength": "25", "totalLength": "100",
         "followedBy": []},
    ])
    result = dl.poll("gid-1")
    assert result.status == PollStatus.PROGRESS
    assert result.progress == 0.25
    assert result.external_id == "gid-2"  # core will retarget the task


def test_poll_unknown_gid_is_permanent_failure():
    dl = make()
    dl._rpc.queue.append(DownloadError("GID 999 is not found"))
    result = dl.poll("gid-999")
    assert result.status == PollStatus.FAILED
    assert result.error_kind == ErrorKind.PERMANENT


def test_poll_rpc_transport_error_propagates_as_exception():
    dl = make()
    dl._rpc.queue.append(DownloadError("aria2 unreachable", ErrorKind.TRANSIENT))
    with pytest.raises(DownloadError):
        dl.poll("gid-1")


# ----------------------------------------------------------------- cancel

def test_cancel_swallows_gone_errors():
    dl = make(responses=[DownloadError("GID is not found")])
    dl.cancel("gid-1")  # must not raise
    assert dl._rpc.calls[0][0] == "remove"
