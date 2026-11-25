from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

from ..core.models import Resource


class CrawlError(RuntimeError):
    """The whole crawl failed (e.g. every page was blocked)."""


class CrawlInterrupted(RuntimeError):
    """A cooperative stop was requested. Batches already ingested stay."""


@dataclass
class CrawlBatch:
    """One increment of crawler output plus row-level progress.

    ``done``/``total`` count source entries (rows), not resources, so the UI
    can show progress even when a row yields no usable mirror. Zero means
    "unknown" for crawlers that cannot report a total.
    """

    resources: list[Resource]
    done: int = 0
    total: int = 0


class CrawlerPlugin(ABC):
    """Fetches candidate resources from one source.

    Contract:
      - Stateless: no results caching, no cursor bookkeeping across calls
        beyond what the source itself allows. The core deduplicates, so
        returning overlapping results is harmless by design.
      - Return raw fields only (title/urls/type/...). Fingerprints, work_key
        and quality are computed by the core during ingest.
      - ``extra`` may carry: ``size`` (bytes; enables content-level dedup of
        re-posts) and ``want_key`` (subscription mode: attach to this want).
      - Long crawls should override ``fetch_batches()`` so the scheduler can
        ingest each batch immediately and stop cooperatively (request_stop).
    """

    type: str = ""

    def __init__(self, name: str, config: dict):
        self.name = name  # instance name = source name in configs
        self.config = config
        self._stop = threading.Event()

    @abstractmethod
    def fetch(self) -> list[Resource]:
        """Return everything found in this pass; the core dedups."""
        raise NotImplementedError

    def fetch_batches(self) -> Iterator[CrawlBatch]:
        """Yield results incrementally. Default: one batch from fetch()."""
        yield CrawlBatch(list(self.fetch()))

    # ------------------------------------------------------- interruption
    def request_stop(self) -> None:
        self._stop.set()

    def reset_stop(self) -> None:
        self._stop.clear()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def sleep(self, seconds: float) -> bool:
        """Sleep unless stop is requested. Returns False when stopped."""
        if seconds <= 0:
            return not self._stop.is_set()
        return not self._stop.wait(seconds)
