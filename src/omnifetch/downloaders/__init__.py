"""Downloader plugin registry."""
from __future__ import annotations

from .base import DownloaderPlugin, DownloadError, PollStatus, TaskResult

_REGISTRY: dict[str, type[DownloaderPlugin]] = {}


def register(cls: type[DownloaderPlugin]) -> type[DownloaderPlugin]:
    _REGISTRY[cls.type] = cls
    return cls


def create_downloader(type_name: str, name: str, config: dict) -> DownloaderPlugin:
    cls = _REGISTRY[type_name]
    return cls(name, config)


def downloader_types() -> list[str]:
    return sorted(_REGISTRY)


from .http import Aria2Downloader  # noqa: E402,F401  (register built-ins)
from .qbittorrent import QBittorrentDownloader  # noqa: E402,F401
