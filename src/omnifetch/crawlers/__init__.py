"""Crawler plugin registry. Plugins are stateless workers: every run of
fetch() must be independent; all bookkeeping lives in the core DB."""
from __future__ import annotations

from .base import CrawlerPlugin

_REGISTRY: dict[str, type[CrawlerPlugin]] = {}


def register(cls: type[CrawlerPlugin]) -> type[CrawlerPlugin]:
    _REGISTRY[cls.type] = cls
    return cls


def create_crawler(type_name: str, name: str, config: dict) -> CrawlerPlugin:
    cls = _REGISTRY[type_name]
    return cls(name, config)


def crawler_types() -> list[str]:
    return sorted(_REGISTRY)


def default_config(type_name: str) -> dict:
    """Field template written into a freshly created plugin instance."""
    cls = _REGISTRY.get(type_name)
    return dict(getattr(cls, "DEFAULT_CONFIG", {})) if cls else {}


from .rss import RssCrawler  # noqa: E402,F401  (register built-ins)
from .vcb import VcbCrawler  # noqa: E402,F401
