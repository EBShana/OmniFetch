from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import Resource


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
    """

    type: str = ""

    def __init__(self, name: str, config: dict):
        self.name = name  # instance name = source name in configs
        self.config = config

    @abstractmethod
    def fetch(self) -> list[Resource]:
        """Return everything found in this pass; the core dedups."""
        raise NotImplementedError
