"""Web UI sessions: HMAC-signed cookie + in-memory login throttle.

The signing key is derived from the stored password hash, so rotating the
password invalidates every existing session and no extra secret file is
needed. Only the hash is ever stored; the plaintext never leaves the login
request.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta

from ..core.security import b64_decode, b64_encode

SESSION_COOKIE = "omnifetch_session"
SESSION_TTL = timedelta(days=7)


def session_secret(password_hash: str) -> bytes:
    return hashlib.sha256(b"omnifetch-ui-session\0" + password_hash.encode()).digest()


def create_session(secret: bytes, username: str, now: datetime,
                   ttl: timedelta = SESSION_TTL) -> str:
    payload = b64_encode(json.dumps(
        {"u": username, "exp": int((now + ttl).timestamp())},
        separators=(",", ":"), sort_keys=True,
    ).encode())
    signature = b64_encode(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_session(secret: bytes, value: str | None, now: datetime) -> str | None:
    if not value or "." not in value:
        return None
    payload, signature = value.rsplit(".", 1)
    expected = b64_encode(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(b64_decode(payload))
        if int(data["exp"]) <= int(now.timestamp()):
            return None
        return str(data["u"])
    except (ValueError, KeyError, TypeError):
        return None


class LoginThrottle:
    """Allow at most ``max_failures`` failures per client within ``window``."""

    def __init__(self, max_failures: int = 5, window: timedelta = timedelta(minutes=5),
                 delay: float = 0.25):
        self.max_failures = max_failures
        self.window = window
        self.delay = delay
        self._failures: dict[str, list[datetime]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str, now: datetime) -> bool:
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if now - t < self.window]
            self._failures[key] = recent
            return len(recent) >= self.max_failures

    def record_failure(self, key: str, now: datetime) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
