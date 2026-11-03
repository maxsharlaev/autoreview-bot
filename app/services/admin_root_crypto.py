"""Root session material: argon2-derived key + HMAC fingerprint (cached in-process)."""

from __future__ import annotations

import hashlib
import hmac
import threading

from argon2.low_level import Type, hash_secret_raw

# Fixed app salt for root key derivation (public; secret is the root password).
_ROOT_ARGON2_SALT = b"opr-admin-root-v1"  # 16 bytes
_ROOT_ARGON2_TIME = 3
_ROOT_ARGON2_MEMORY = 65536
_ROOT_ARGON2_PARALLELISM = 1
_ROOT_KEY_LEN = 32
_ROOT_HMAC_MSG = b"root-session"

_lock = threading.Lock()
_cache_creds: tuple[str, str] | None = None
_cache_fingerprint: str | None = None


def _derive_root_key(login: str, password: str) -> bytes:
    material = f"{login}\0{password}".encode()
    return hash_secret_raw(
        secret=material,
        salt=_ROOT_ARGON2_SALT,
        time_cost=_ROOT_ARGON2_TIME,
        memory_cost=_ROOT_ARGON2_MEMORY,
        parallelism=_ROOT_ARGON2_PARALLELISM,
        hash_len=_ROOT_KEY_LEN,
        type=Type.ID,
    )


def fingerprint_from_key(key: bytes) -> str:
    return hmac.new(key, _ROOT_HMAC_MSG, hashlib.sha256).hexdigest()


def compute_root_fingerprint(login: str, password: str) -> str:
    """Derive argon2 key then HMAC; expensive — prefer get_root_fingerprint()."""
    return fingerprint_from_key(_derive_root_key(login, password))


def get_root_fingerprint(login: str, password: str) -> str:
    """Return cached fingerprint for (login, password); recompute on change."""
    global _cache_creds, _cache_fingerprint
    creds = (login, password)
    with _lock:
        if _cache_creds == creds and _cache_fingerprint is not None:
            return _cache_fingerprint
        fp = compute_root_fingerprint(login, password)
        _cache_creds = creds
        _cache_fingerprint = fp
        return fp


def clear_root_fingerprint_cache() -> None:
    global _cache_creds, _cache_fingerprint
    with _lock:
        _cache_creds = None
        _cache_fingerprint = None


def warm_root_fingerprint_cache(login: str, password: str) -> str:
    """Compute and cache at startup. Returns the fingerprint."""
    clear_root_fingerprint_cache()
    return get_root_fingerprint(login, password)
