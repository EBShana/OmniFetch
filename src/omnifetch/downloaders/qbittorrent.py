from __future__ import annotations

from ..core.models import ResourceType
from . import register
from .base import DownloaderPlugin


@register
class QBittorrentDownloader(DownloaderPlugin):
    """qBittorrent via its Web API. Config keys:

      url       e.g. http://qbittorrent:8080 (required)
      username / password

    SKELETON — module to be implemented later:
      submit  -> api/v2/torrents/add (torrent file / magnet / url);
                 external_id = infohash
      poll    -> api/v2/torrents/info?hashes=...; map state to TaskResult
      exists  -> the infohash check BEFORE add: the last line of defence
                 against duplicate downloads, even if the DB is reset
      cancel  -> api/v2/torrents/delete?deleteFiles=true
    """

    type = "qbittorrent"
    accepts = (ResourceType.TORRENT, ResourceType.MAGNET, ResourceType.HTTP)
    DEFAULT_CONFIG = {"url": "", "username": "", "password": ""}

    def submit(self, resource):
        raise NotImplementedError("qbittorrent module not implemented yet")

    def poll(self, external_id: str):
        raise NotImplementedError("qbittorrent module not implemented yet")
