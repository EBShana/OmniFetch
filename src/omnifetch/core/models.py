"""Core data model.

These dataclasses are the stable contract between plugins and the management
core. All persisted data is owned by the core; plugins are stateless. Keep
every field plugin-agnostic so swapping a crawler/downloader never invalidates
stored rows (only ``extra``/``external_id`` become opaque, never lost).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ResourceType(str, Enum):
    TORRENT = "torrent"
    MAGNET = "magnet"
    HTTP = "http"
    NETDISK = "netdisk"


class WantStatus(str, Enum):
    WANTED = "WANTED"
    ACQUIRED = "ACQUIRED"
    GIVEN_UP = "GIVEN_UP"


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"  # transient failure, waiting for backoff
    FAILED = "FAILED"                      # terminal: permanent error or retries exhausted
    COMPLETED = "COMPLETED"                # terminal
    IGNORED = "IGNORED"                    # terminal: manually dismissed


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.COMPLETED, TaskStatus.IGNORED}
)
ACTIVE_TASK_STATUSES = frozenset(
    {TaskStatus.QUEUED, TaskStatus.DOWNLOADING, TaskStatus.FAILED_RETRYABLE}
)


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"


# Resource lifecycle: NEW -> TASKED; REDUNDANT = another candidate of the want won.
RESOURCE_NEW = "NEW"
RESOURCE_TASKED = "TASKED"
RESOURCE_REDUNDANT = "REDUNDANT"


@dataclass
class Quality:
    """Structured fields parsed from the title; used to rank candidates."""

    resolution: int | None = None  # vertical pixels, e.g. 1080
    source: str | None = None      # bluray, web-dl, webrip, hdtv, ...
    codec: str | None = None       # x264, x265, hevc, av1, ...
    season: int | None = None
    episode: int | None = None
    year: int | None = None
    group: str | None = None       # release group / fansub

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, data: str | None) -> "Quality":
        if not data:
            return cls()
        return cls(**json.loads(data))


@dataclass
class Resource:
    """One downloadable release (one version of a work)."""

    # --- fields a crawler plugin provides ---
    title: str = ""
    source_url: str = ""
    source: str = ""  # site identifier, e.g. "nyaa", "mteam"
    download_uris: list[str] = field(default_factory=list)
    resource_type: ResourceType = ResourceType.HTTP
    published_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # --- fields the core fills in during ingest ---
    id: int | None = None
    normalized_title: str = ""
    fingerprint: str = ""
    content_fingerprint: str = ""
    work_key: str = ""
    want_id: int | None = None
    quality: Quality = field(default_factory=Quality)
    status: str = RESOURCE_NEW


@dataclass
class Want:
    """A work/demand: acquire any of its candidate resources, in rank order."""

    work_key: str
    display_title: str = ""
    status: WantStatus = WantStatus.WANTED
    match_rule: dict[str, Any] = field(default_factory=dict)
    rank_rule: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    acquired_resource_id: int | None = None


@dataclass
class Task:
    """Download of one resource by one downloader."""

    resource_id: int
    downloader: str
    id: int | None = None
    status: TaskStatus = TaskStatus.QUEUED
    retry_count: int = 0
    next_retry_at: datetime | None = None
    claimed_at: datetime | None = None
    last_progress_at: datetime | None = None
    external_id: str | None = None
    progress: float = 0.0
    error_kind: ErrorKind | None = None
    error_message: str | None = None
