"""Argon2 password hashing for admin users."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher()

# DB admin users
MIN_ADMIN_PASSWORD_LENGTH = 12
# Env-only root (startup validation)
MIN_ROOT_PASSWORD_LENGTH = 16


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_too_short(password: str, *, minimum: int = MIN_ADMIN_PASSWORD_LENGTH) -> bool:
    return len(password or "") < minimum


def root_password_too_short(password: str) -> bool:
    return password_too_short(password, minimum=MIN_ROOT_PASSWORD_LENGTH)
