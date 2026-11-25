"""Settings and plugin configuration.

Config files are TOML (stdlib tomllib), split into two directories:
  <config_dir>/sources.d/*.toml       one per crawling source
  <config_dir>/downloaders.d/*.toml   one per downloader instance

Every file must contain ``type`` (plugin type) and ``enabled``; the rest of
the keys are passed verbatim to the plugin as its config dict.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .core.security import hash_password
from .core.statemachine import RetryPolicy

ENV_PREFIX = "OMNIFETCH_"
LEGACY_ENV_PREFIX = "OMNFETCH_"  # old spelling, still accepted as fallback
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
    dispatch_daily_limit: int = 0         # global cap, 0 = unlimited (per-downloader caps win)
    webhook_url: str = ""                 # set to enable webhook notifications
    token: str = ""                       # set to require auth on API/UI data
    ui_username: str = "admin"            # Web UI login user
    ui_password_hash: str = ""            # scrypt hash; empty = UI login disabled
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
    if "dispatch_daily_limit" in data:
        settings.dispatch_daily_limit = int(data["dispatch_daily_limit"])
    if "webhook_url" in data:
        settings.webhook_url = str(data["webhook_url"])
    if "token" in data:
        settings.token = str(data["token"])
    if "ui_username" in data:
        settings.ui_username = str(data["ui_username"])
    if "ui_password_hash" in data:
        settings.ui_password_hash = str(data["ui_password_hash"])
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
        "dispatch_daily_limit": settings.dispatch_daily_limit,
        "webhook_url": settings.webhook_url,
        "token": settings.token,
        "retry_max_retries": settings.retry.max_retries,
        "retry_base_seconds": settings.retry.base_seconds,
        "retry_max_seconds": settings.retry.max_seconds,
    }


def _toml_value(key: str, value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Newlines/tabs would break a TOML basic string unless escaped.
        escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        return f'"{escaped}"'
    raise ValueError(f"unsupported TOML value for {key}: {type(value).__name__}")


def dump_toml(data: dict) -> str:
    """Minimal TOML writer — flat key = value pairs plus flat lists."""
    lines = []
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            inner = ", ".join(_toml_value(key, item) for item in value)
            lines.append(f"{key} = [{inner}]")
        else:
            lines.append(f"{key} = {_toml_value(key, value)}")
    return "\n".join(lines) + "\n"


def save_core_settings(config_dir: str, data: dict) -> None:
    path = Path(config_dir) / CORE_TOML
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(data)
    if path.is_file():
        # Keep keys the settings form does not expose (e.g. ui_password_hash)
        # instead of wiping them on the next save.
        with path.open("rb") as fh:
            for key, value in tomllib.load(fh).items():
                merged.setdefault(key, value)
    path.write_text(dump_toml(merged), encoding="utf-8")


_PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def _plugin_path(config_dir: str, kind: str, name: str) -> Path:
    if kind not in ("source", "downloader"):
        raise ValueError(f"invalid plugin kind: {kind!r}")
    if not name or ".." in name or not _PLUGIN_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid plugin name: {name!r}")
    directory = Path(config_dir) / ("sources.d" if kind == "source" else "downloaders.d")
    return directory / f"{name}.toml"


def save_plugin_config(config_dir: str, kind: str, name: str, data: dict) -> None:
    path = _plugin_path(config_dir, kind, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(data), encoding="utf-8")


def remove_plugin_config(config_dir: str, kind: str, name: str) -> bool:
    path = _plugin_path(config_dir, kind, name)
    if path.is_file():
        path.unlink()
        return True
    return False


def load_settings() -> Settings:
    def env(name: str, default: str = "") -> str:
        value = os.environ.get(ENV_PREFIX + name)
        if value is None:  # OMNIFETCH_ wins; OMNFETCH_ stays for old deploys
            value = os.environ.get(LEGACY_ENV_PREFIX + name)
        return default if value is None else value

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
    if env("DISPATCH_DAILY_LIMIT"):
        settings.dispatch_daily_limit = int(env("DISPATCH_DAILY_LIMIT"))
    if env("WEBHOOK"):
        settings.webhook_url = env("WEBHOOK")
    if env("TOKEN"):
        settings.token = env("TOKEN")
    if env("UI_USERNAME"):
        settings.ui_username = env("UI_USERNAME")
    if env("UI_PASSWORD_HASH"):
        settings.ui_password_hash = env("UI_PASSWORD_HASH")
    elif env("UI_PASSWORD"):
        # Convenience for Docker/NAS: the plaintext env var is hashed in
        # memory at startup and never written to a config file.
        settings.ui_password_hash = hash_password(env("UI_PASSWORD"))
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
