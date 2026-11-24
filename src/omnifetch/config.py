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
CORE_TOML = "omnifetch.toml"  # core settings, lives inside the config dir


@dataclass
class Settings:
    db_path: str = "./data/omnifetch.db"
    config_dir: str = "./config"
    poll_interval: int = 30               # scheduler loop period, seconds
    reaper_stale: timedelta = timedelta(minutes=10)
    stalled_after: timedelta = timedelta(minutes=15)  # no progress => transient failure
    max_dispatch_per_tick: int = 10
    max_active_downloads: int = 3         # global concurrency cap for downloads
    webhook_url: str = ""                 # set to enable webhook notifications
    token: str = ""                       # set to require auth on API/UI data
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def _apply_core_dict(settings: Settings, data: dict) -> None:
    """Apply a flat core-settings dict (from core.toml or the settings UI)."""
    if "poll_interval" in data:
        settings.poll_interval = int(data["poll_interval"])
    if "reaper_stale_minutes" in data:
        settings.reaper_stale = timedelta(minutes=int(data["reaper_stale_minutes"]))
    if "stalled_after_minutes" in data:
        settings.stalled_after = timedelta(minutes=int(data["stalled_after_minutes"]))
    if "max_dispatch_per_tick" in data:
        settings.max_dispatch_per_tick = int(data["max_dispatch_per_tick"])
    if "max_active_downloads" in data:
        settings.max_active_downloads = int(data["max_active_downloads"])
    if "webhook_url" in data:
        settings.webhook_url = str(data["webhook_url"])
    if "token" in data:
        settings.token = str(data["token"])
    if "retry_max_retries" in data:
        settings.retry.max_retries = int(data["retry_max_retries"])
    if "retry_base_seconds" in data:
        settings.retry.base_seconds = int(data["retry_base_seconds"])
    if "retry_max_seconds" in data:
        settings.retry.max_seconds = int(data["retry_max_seconds"])


def core_settings_dict(settings: Settings) -> dict:
    """Round-trippable flat dict of the editable core settings (for the UI)."""
    return {
        "poll_interval": settings.poll_interval,
        "reaper_stale_minutes": int(settings.reaper_stale.total_seconds() // 60),
        "stalled_after_minutes": int(settings.stalled_after.total_seconds() // 60),
        "max_dispatch_per_tick": settings.max_dispatch_per_tick,
        "max_active_downloads": settings.max_active_downloads,
        "webhook_url": settings.webhook_url,
        "token": settings.token,
        "retry_max_retries": settings.retry.max_retries,
        "retry_base_seconds": settings.retry.base_seconds,
        "retry_max_seconds": settings.retry.max_seconds,
    }


def dump_toml(data: dict) -> str:
    """Minimal TOML writer — our configs are flat key = value pairs."""
    lines = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        elif isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        else:
            raise ValueError(f"unsupported TOML value for {key}: {type(value).__name__}")
    return "\n".join(lines) + "\n"


def save_core_settings(config_dir: str, data: dict) -> None:
    path = Path(config_dir) / CORE_TOML
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(data), encoding="utf-8")


def save_plugin_config(config_dir: str, kind: str, name: str, data: dict) -> None:
    directory = Path(config_dir) / ("sources.d" if kind == "source" else "downloaders.d")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(dump_toml(data), encoding="utf-8")


def remove_plugin_config(config_dir: str, kind: str, name: str) -> bool:
    path = (Path(config_dir)
            / ("sources.d" if kind == "source" else "downloaders.d")
            / f"{name}.toml")
    if path.is_file():
        path.unlink()
        return True
    return False


def load_settings() -> Settings:
    def env(name: str, default: str = "") -> str:
        return os.environ.get(ENV_PREFIX + name, default)

    settings = Settings(config_dir=env("CONFIG", "./config"))
    core_path = Path(settings.config_dir) / CORE_TOML
    if core_path.is_file():
        with core_path.open("rb") as fh:
            _apply_core_dict(settings, tomllib.load(fh))

    # environment variables override core.toml (keeps the Docker workflow)
    settings.db_path = env("DB", settings.db_path)
    if env("POLL_INTERVAL"):
        settings.poll_interval = int(env("POLL_INTERVAL"))
    if env("REAPER_STALE_MINUTES"):
        settings.reaper_stale = timedelta(minutes=int(env("REAPER_STALE_MINUTES")))
    if env("STALLED_AFTER_MINUTES"):
        settings.stalled_after = timedelta(minutes=int(env("STALLED_AFTER_MINUTES")))
    if env("MAX_ACTIVE_DOWNLOADS"):
        settings.max_active_downloads = int(env("MAX_ACTIVE_DOWNLOADS"))
    if env("WEBHOOK"):
        settings.webhook_url = env("WEBHOOK")
    if env("TOKEN"):
        settings.token = env("TOKEN")
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
