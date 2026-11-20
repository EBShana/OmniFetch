"""Settings and plugin configuration.

Config files are TOML (stdlib tomllib), split into two directories:
  <config_dir>/sources.d/*.toml       one per crawling source
  <config_dir>/downloaders.d/*.toml   one per downloader instance

Every file must contain ``type`` (plugin type) and ``enabled``; the rest of
the keys are passed verbatim to the plugin as its config dict.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .core.statemachine import RetryPolicy

ENV_PREFIX = "OMNFETCH_"


@dataclass
class Settings:
    db_path: str = "./data/omnifetch.db"
    config_dir: str = "./config"
    poll_interval: int = 30               # scheduler loop period, seconds
    reaper_stale: timedelta = timedelta(minutes=10)
    max_dispatch_per_tick: int = 10
    webhook_url: str = ""                 # set to enable webhook notifications
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def load_settings() -> Settings:
    def env(name: str, default: str = "") -> str:
        return os.environ.get(ENV_PREFIX + name, default)

    settings = Settings(
        db_path=env("DB", "./data/omnifetch.db"),
        config_dir=env("CONFIG", "./config"),
        poll_interval=int(env("POLL_INTERVAL", "30")),
        webhook_url=env("WEBHOOK", ""),
    )
    if env("REAPER_STALE_MINUTES"):
        settings.reaper_stale = timedelta(minutes=int(env("REAPER_STALE_MINUTES")))
    return settings


def _load_toml_dir(directory: Path) -> dict[str, dict]:
    configs: dict[str, dict] = {}
    if not directory.is_dir():
        return configs
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        name = data.get("name") or path.stem
        data.setdefault("name", name)
        configs[name] = data
    return configs


def load_plugin_configs(config_dir: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (sources, downloaders): name -> config, disabled entries included."""
    root = Path(config_dir)
    sources = _load_toml_dir(root / "sources.d")
    downloaders = _load_toml_dir(root / "downloaders.d")
    return sources, downloaders


def enabled(configs: dict[str, dict]) -> dict[str, dict]:
    return {name: cfg for name, cfg in configs.items() if cfg.get("enabled", True)}
