"""Argon2 password hashing for admin users."""

from __future__ import annotations

import hashlib
import hmac
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher()

# DB admin users
MIN_ADMIN_PASSWORD_LENGTH = 12
# Env-only root (startup validation)
MIN_ROOT_PASSWORD_LENGTH = 16


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


# Root password hashed once per process with the same hasher, so root login
# goes through verify_password like DB users (one argon2 verify per attempt).
_root_hash_lock = threading.Lock()
_root_hash_cache: tuple[bytes, str] | None = None  # (sha256(password), argon2 hash)


def get_root_password_hash(password: str) -> str:
    global _root_hash_cache
    digest = hashlib.sha256(password.encode()).digest()
    with _root_hash_lock:
        if _root_hash_cache is not None and hmac.compare_digest(_root_hash_cache[0], digest):
            return _root_hash_cache[1]
        _root_hash_cache = (digest, _HASHER.hash(password))
        return _root_hash_cache[1]


def clear_root_password_hash() -> None:
    global _root_hash_cache
    with _root_hash_lock:
        _root_hash_cache = None


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_too_short(password: str, *, minimum: int = MIN_ADMIN_PASSWORD_LENGTH) -> bool:
    return len(password or "") < minimum


def root_password_too_short(password: str) -> bool:
    return password_too_short(password, minimum=MIN_ROOT_PASSWORD_LENGTH)
