"""Password hashing for the Web UI login (stdlib scrypt, no extra deps).

Hashes look like ``scrypt$16384$8$1$<salt>$<digest>`` so they can live in
``config/omnifetch.toml`` or an environment variable without storing the
plaintext password anywhere on disk.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
    )
    return (f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
            f"${b64_encode(salt)}${b64_encode(derived)}")


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check; malformed hashes simply fail."""
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected_raw = b64_decode(expected)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=b64_decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected_raw),
        )
        return hmac.compare_digest(derived, expected_raw)
    except (ValueError, TypeError):
        return False
