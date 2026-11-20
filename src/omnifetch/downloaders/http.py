from __future__ import annotations

from ..core.models import ResourceType
from .base import DownloaderPlugin


class Aria2Downloader(DownloaderPlugin):
    """aria2 via JSON-RPC. Config keys:

      rpc_url      e.g. http://aria2:6800/jsonrpc (required)
      rpc_secret   aria2 rpc-secret token (optional)
      download_dir destination directory inside aria2's file system (optional)

    SKELETON — module to be implemented next:
      submit  -> aria2.addUri, return gid as external_id
      poll    -> aria2.tellStatus; map status to TaskResult
                 (active/waiting=PROGRESS, complete=DONE,
                  error=FAILED with permanent/transient mapping,
                  removed=FAILED permanent)
      cancel  -> aria2.remove + aria2.removeDownloadResult
      exists  -> aria2.tellStatus(gid) — only if we derive gid deterministically;
                 otherwise rely on the core's DB + completed-file check
    """

    type = "aria2"
    accepts = (ResourceType.HTTP, ResourceType.TORRENT, ResourceType.MAGNET)

    def submit(self, resource):
        raise NotImplementedError("aria2 module not implemented yet")

    def poll(self, external_id: str):
        raise NotImplementedError("aria2 module not implemented yet")
