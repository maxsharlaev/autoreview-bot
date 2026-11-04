"""Root session material: argon2-derived key + HMAC fingerprint (computed once at startup)."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw

# Fixed app salt for root key derivation (public; secret is the root password).
_ROOT_ARGON2_SALT = b"opr-admin-root-v1"  # 16 bytes
_ROOT_ARGON2_TIME = 3
_ROOT_ARGON2_MEMORY = 65536
_ROOT_ARGON2_PARALLELISM = 1
_ROOT_KEY_LEN = 32
_ROOT_HMAC_MSG = b"root-session"


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
    """Derive argon2 key then HMAC; expensive — done once in load_root_credentials()."""
    return fingerprint_from_key(_derive_root_key(login, password))


@dataclass(frozen=True)
class RootCredentials:
    """Root material computed once at startup; root changes need .env + restart."""

    login: str
    password_hash: str  # argon2 (same hasher as DB users)
    fingerprint: str  # session fingerprint


_root: RootCredentials | None = None


def load_root_credentials(login: str, password: str) -> RootCredentials:
    """Expensive (two argon2 runs); call once at startup, off the event loop."""
    global _root
    from app.services.admin_passwords import hash_password

    _root = RootCredentials(
        login=login,
        password_hash=hash_password(password),
        fingerprint=compute_root_fingerprint(login, password),
    )
    return _root


def get_root_credentials() -> RootCredentials | None:
    """Cheap synchronous read; None means root is not configured (non-root login only)."""
    return _root


def clear_root_credentials() -> None:
    global _root
    _root = None
