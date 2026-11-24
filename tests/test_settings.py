"""Settings page: core settings persistence + live-apply, plugin config
edit / add / delete with hot reload."""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnifetch.app import create_app
from omnifetch.config import CORE_TOML, Settings, dump_toml, load_settings
from omnifetch.notify import WebhookNotifier
from omnifetch.scheduler.runner import build_context

SOURCE_TOML = """\
name = "demo-rss"
type = "rss"
enabled = false
url = "https://example.com/feed.xml"
interval = 900
resource_type = "http"
"""


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "sources.d").mkdir(parents=True)
    (cfg / "downloaders.d").mkdir()
    (cfg / "sources.d" / "demo-rss.toml").write_text(SOURCE_TOML, encoding="utf-8")
    settings = Settings(db_path=str(tmp_path / "t.db"), config_dir=str(cfg))
    app = create_app(settings)
    return app, settings


@pytest.fixture
def client(env):
    return TestClient(env[0])


def test_settings_fragment_renders_core_and_instances(client, env):
    html = client.get("/frag/settings").text
    assert "核心设置" in html
    assert "调度轮询间隔" in html
    assert "demo-rss" in html
    assert "https://example.com/feed.xml" in html
    assert "爬取源" in html and "下载器" in html


def test_core_settings_save_persists_and_applies(client, env):
    app, settings = env
    response = client.post("/ui/settings/core", data={
        "poll_interval": "60",
        "reaper_stale_minutes": "20",
        "stalled_after_minutes": "30",
        "max_dispatch_per_tick": "5",
        "max_active_downloads": "2",
        "webhook_url": "",
        "token": "",
        "retry_max_retries": "3",
        "retry_base_seconds": "30",
        "retry_max_seconds": "300",
    })
    assert response.status_code == 200
    text = (Path(settings.config_dir) / CORE_TOML).read_text(encoding="utf-8")
    assert "poll_interval = 60" in text
    assert "max_active_downloads = 2" in text
    # live-applied to the running context
    ctx = app.state.ctx
    assert ctx.settings.poll_interval == 60
    assert ctx.settings.max_active_downloads == 2
    assert ctx.settings.retry.max_retries == 3
    assert ctx.settings.stalled_after.total_seconds() == 1800


def test_plugin_save_updates_file_and_hot_reloads(client, env):
    app, settings = env
    response = client.post("/ui/settings/source/demo-rss", data={
        "type": "rss",
        "enabled": "on",
        "url": "https://example.com/new.xml",
        "interval": "1800",
        "resource_type": "magnet",
    })
    assert response.status_code == 200
    trigger = json.loads(response.headers["hx-trigger"])
    assert "已保存并启用" in trigger["toast"]
    assert "demo-rss" in app.state.ctx.crawlers
    assert app.state.ctx.crawlers["demo-rss"].config["interval"] == 1800

    # disable again
    response = client.post("/ui/settings/source/demo-rss", data={
        "type": "rss", "url": "https://example.com/new.xml",
        "interval": "1800", "resource_type": "magnet",
    })
    trigger = json.loads(response.headers["hx-trigger"])
    assert "已保存（停用）" in trigger["toast"]
    assert "demo-rss" not in app.state.ctx.crawlers


def test_add_and_delete_instance(client, env):
    app, settings = env
    response = client.post("/ui/settings/add", data={
        "kind": "downloader", "type": "aria2", "name": "my-aria2",
    })
    assert response.status_code == 200
    created = Path(settings.config_dir) / "downloaders.d" / "my-aria2.toml"
    assert created.is_file()
    assert 'enabled = false' in created.read_text(encoding="utf-8")

    response = client.post("/ui/settings/delete/downloader/my-aria2")
    assert response.status_code == 200
    assert not created.is_file()


def test_settings_fragments_require_auth(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    settings = Settings(db_path=str(tmp_path / "t.db"), config_dir=str(cfg),
                        token="tk")
    client = TestClient(create_app(settings))
    assert client.get("/frag/settings").status_code == 401
    assert client.post("/ui/settings/core", data={"poll_interval": "60"}).status_code == 401
    headers = {"X-OmniFetch-Token": "tk"}
    assert client.get("/frag/settings", headers=headers).status_code == 200
    assert client.get("/settings").status_code == 200  # shell open


def test_add_rejects_unknown_type(client):
    response = client.post("/ui/settings/add", data={
        "kind": "source", "type": "no-such-plugin", "name": "x",
    })
    assert response.status_code == 400
