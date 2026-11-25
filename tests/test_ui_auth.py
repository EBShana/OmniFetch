"""Web UI login: password hashing, signed sessions, throttle, guard wiring."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from omnifetch.app import create_app
from omnifetch.config import (CORE_TOML, Settings, load_settings,
                              save_core_settings)
from omnifetch.core.security import hash_password, verify_password
from omnifetch.webui.auth import (SESSION_COOKIE, LoginThrottle, create_session,
                                  session_secret, verify_session)

PASSWORD = "correct horse"


# --------------------------------------------------------------- password


def test_password_hash_roundtrip_and_no_plaintext():
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$")
    assert PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded) is True
    assert verify_password("wrong", encoded) is False
    # Salted: the same password produces a different hash twice.
    assert hash_password(PASSWORD) != encoded


def test_verify_password_rejects_malformed_hashes():
    assert verify_password(PASSWORD, "") is False
    assert verify_password(PASSWORD, "plain-text") is False
    assert verify_password(PASSWORD, "bcrypt$1$2$3$4$5") is False


# ---------------------------------------------------------------- session


def test_session_roundtrip_and_tamper():
    secret = session_secret(hash_password(PASSWORD))
    now = datetime(2026, 1, 1, 12, 0, 0)
    cookie = create_session(secret, "admin", now)
    assert verify_session(secret, cookie, now) == "admin"
    assert verify_session(secret, cookie + "x", now) is None
    assert verify_session(secret, cookie, now + timedelta(days=8)) is None
    # Rotating the password invalidates the cookie.
    other = session_secret(hash_password("new password"))
    assert verify_session(other, cookie, now) is None


def test_login_throttle_blocks_after_max_failures():
    throttle = LoginThrottle(max_failures=3, window=timedelta(minutes=5),
                             delay=0)
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert throttle.blocked("1.2.3.4", now) is False
    for _ in range(3):
        throttle.record_failure("1.2.3.4", now)
    assert throttle.blocked("1.2.3.4", now) is True
    assert throttle.blocked("5.6.7.8", now) is False  # per-client buckets
    throttle.reset("1.2.3.4")
    assert throttle.blocked("1.2.3.4", now) is False
    # Failures age out of the window.
    throttle.record_failure("9.9.9.9", now)
    throttle.record_failure("9.9.9.9", now)
    assert throttle.blocked("9.9.9.9", now + timedelta(minutes=6)) is False


# ------------------------------------------------------------- integration


@pytest.fixture
def env(tmp_path):
    (tmp_path / "cfg").mkdir()
    settings = Settings(
        db_path=str(tmp_path / "t.db"), config_dir=str(tmp_path / "cfg"),
        ui_username="admin", ui_password_hash=hash_password(PASSWORD),
    )
    return create_app(settings), settings


@pytest.fixture
def client(env):
    return TestClient(env[0])


def _login(client, *, username="admin", password=PASSWORD, next_path="/"):
    return client.post("/login", data={
        "username": username, "password": password, "next": next_path,
    }, follow_redirects=False)


def test_pages_and_fragments_redirect_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/"

    response = client.get("/frag/settings", headers={"HX-Request": "true"},
                          follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"
    assert response.headers["HX-Redirect"] != "/"


def test_login_page_renders_and_success_sets_cookie(client):
    page = client.get("/login")
    assert page.status_code == 200
    assert "用户名" in page.text and 'name="password"' in page.text

    response = _login(client, next_path="/tasks")
    assert response.status_code == 303
    assert response.headers["location"] == "/tasks"
    session = response.cookies.get(SESSION_COOKIE)
    assert session and "." in session
    # Session unlocks both pages and fragments.
    assert client.get("/").status_code == 200
    assert client.get("/frag/settings").status_code == 200


def test_login_rejects_bad_password_and_throttles(client, monkeypatch):
    monkeypatch.setattr("omnifetch.webui.routes.time.sleep", lambda *_: None)
    for _ in range(5):
        response = _login(client, password="nope")
        assert response.status_code == 401
        assert "不正确" in response.text
    blocked = _login(client, password="nope")
    assert blocked.status_code == 429
    assert "尝试次数过多" in blocked.text
    # A correct password does not bypass an active lock either.
    assert _login(client).status_code == 429


def test_login_next_cannot_redirect_offsite(client):
    response = _login(client, next_path="https://evil.example/steal")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    client.cookies.clear()
    response = _login(client, next_path="//evil.example")
    assert response.headers["location"] == "/"


def test_logout_clears_session(client):
    _login(client)
    assert client.get("/").status_code == 200
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_api_keeps_token_auth_after_ui_login(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "t.db"), config_dir=str(tmp_path / "cfg"),
        token="api-token", ui_password_hash=hash_password(PASSWORD),
    )
    client = TestClient(create_app(settings))
    # Logged-in UI sessions do not grant API access.
    _login(client)
    assert client.get("/api/auth/verify").status_code == 401
    assert client.get("/api/auth/verify",
                      headers={"X-OmniFetch-Token": "api-token"}
                      ).status_code == 200
    # API calls are never redirected to the login page.
    client.cookies.clear()
    response = client.get("/api/auth/verify", follow_redirects=False)
    assert response.status_code == 401


def test_ui_login_disabled_when_hash_empty(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"),
                        config_dir=str(tmp_path / "cfg"))
    client = TestClient(create_app(settings))
    assert client.get("/").status_code == 200
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# ----------------------------------------------------------------- config


def _clear_omnifetch_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("OMNIFETCH_", "OMNFETCH_")):
            monkeypatch.delenv(key)


def test_load_settings_reads_ui_hash_from_toml(monkeypatch, tmp_path):
    _clear_omnifetch_env(monkeypatch)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    encoded = hash_password(PASSWORD)
    (cfg / CORE_TOML).write_text(
        f'ui_username = "boss"\nui_password_hash = "{encoded}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIFETCH_CONFIG", str(cfg))
    settings = load_settings()
    assert settings.ui_username == "boss"
    assert verify_password(PASSWORD, settings.ui_password_hash) is True


def test_ui_password_env_is_hashed_in_memory(monkeypatch, tmp_path):
    _clear_omnifetch_env(monkeypatch)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("OMNIFETCH_CONFIG", str(cfg))
    monkeypatch.setenv("OMNIFETCH_UI_PASSWORD", PASSWORD)
    settings = load_settings()
    assert settings.ui_password_hash.startswith("scrypt$")
    assert verify_password(PASSWORD, settings.ui_password_hash) is True
    # The plaintext never lands in a config file.
    assert not (cfg / CORE_TOML).exists()


def test_env_hash_takes_precedence_over_plaintext(monkeypatch, tmp_path):
    _clear_omnifetch_env(monkeypatch)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    encoded = hash_password("from-env-hash")
    monkeypatch.setenv("OMNIFETCH_CONFIG", str(cfg))
    monkeypatch.setenv("OMNIFETCH_UI_PASSWORD", "ignored")
    monkeypatch.setenv("OMNIFETCH_UI_PASSWORD_HASH", encoded)
    assert load_settings().ui_password_hash == encoded


def test_saving_core_settings_keeps_ui_hash(tmp_path):
    encoded = hash_password(PASSWORD)
    (tmp_path / CORE_TOML).write_text(
        f'ui_password_hash = "{encoded}"\npoll_interval = 30\n', encoding="utf-8")
    save_core_settings(str(tmp_path), {"poll_interval": 60})
    saved = (tmp_path / CORE_TOML).read_text(encoding="utf-8")
    assert "poll_interval = 60" in saved
    assert encoded in saved


# -------------------------------------------------------------------- cli


def test_hash_password_cli_prints_hash(monkeypatch, tmp_path, capsys):
    from omnifetch.__main__ import main

    _clear_omnifetch_env(monkeypatch)
    (tmp_path / "cfg").mkdir()
    monkeypatch.setenv("OMNIFETCH_CONFIG", str(tmp_path / "cfg"))
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(answers))
    assert main(["hash-password"]) == 0
    printed = capsys.readouterr().out.strip().splitlines()[0]
    assert printed.startswith("scrypt$")
    assert verify_password(PASSWORD, printed) is True


def test_hash_password_cli_rejects_mismatch(monkeypatch, tmp_path):
    from omnifetch.__main__ import main

    _clear_omnifetch_env(monkeypatch)
    (tmp_path / "cfg").mkdir()
    monkeypatch.setenv("OMNIFETCH_CONFIG", str(tmp_path / "cfg"))
    answers = iter(["one", "two"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(answers))
    assert main(["hash-password"]) == 1
