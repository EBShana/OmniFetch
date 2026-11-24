from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from ..core.models import ErrorKind, Resource, ResourceType


class PollStatus(str, Enum):
    PROGRESS = "PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass
class TaskResult:
    """What poll() reports for one external task."""

    status: PollStatus
    progress: float = 0.0
    error_kind: ErrorKind | None = None      # required when status == FAILED
    error_message: str | None = None
    # Some tools replace the job handle mid-flight (aria2: the magnet/.torrent
    # fetch completes and spawns a followedBy download). When set and
    # different from the current handle, the core updates tasks.external_id.
    external_id: str | None = None


class DownloadError(Exception):
    """Raised by submit/poll/cancel. ``kind`` tells the core whether the
    failure is worth retrying (transient) or terminal (permanent)."""

    def __init__(self, message: str, kind: ErrorKind = ErrorKind.TRANSIENT):
        super().__init__(message)
        self.kind = kind


class DownloaderPlugin(ABC):
    """Executes downloads via one tool (aria2, qBittorrent, netdisk client...).

    Contract:
      - Stateless: the downloader tool itself is the source of truth for
        in-flight jobs; this plugin only maps between the core's Task model
        and the tool's API.
      - ``submit`` must be idempotent-safe: check ``exists()`` first where
        the tool allows it (e.g. infohash already in the client) so a core
        restart never creates a duplicate download.
      - Failures are reported via TaskResult.error_kind, never by raising
        for *job-level* failures (a dead torrent is a result, not an
        exception). Raise DownloadError only for API-level problems.
    """

    type: str = ""
    accepts: tuple[ResourceType, ...] = ()

    def __init__(self, name: str, config: dict):
        self.name = name  # instance name = downloader name in configs/tasks
        self.config = config

    def supports(self, resource: Resource) -> bool:
        return resource.resource_type in self.accepts

    @abstractmethod
    def submit(self, resource: Resource) -> str:
        """Start the download; return the tool-side handle (external_id)."""
        raise NotImplementedError

    @abstractmethod
    def poll(self, external_id: str) -> TaskResult:
        """Report progress / completion / failure of one external task."""
        raise NotImplementedError

    def cancel(self, external_id: str) -> None:
        """Best-effort cancel; used when a chain falls back or on reaping."""

    def exists(self, resource: Resource) -> bool:
        """Optional last line of defence against duplicate submission."""
        return False

    def ping(self) -> tuple[bool, str]:
        """Liveness probe for the system status page. Returns (ok, detail)."""
        return False, "未实现"
