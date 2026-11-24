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


def default_config(type_name: str) -> dict:
    """Field template written into a freshly created plugin instance."""
    cls = _REGISTRY.get(type_name)
    return dict(getattr(cls, "DEFAULT_CONFIG", {})) if cls else {}


from .http import Aria2Downloader  # noqa: E402,F401  (register built-ins)
from .p115 import P115Downloader  # noqa: E402,F401
from .qbittorrent import QBittorrentDownloader  # noqa: E402,F401
