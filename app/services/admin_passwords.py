"""Argon2 password hashing for admin users."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable

import anyio
import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher()

# DB admin users
MIN_ADMIN_PASSWORD_LENGTH = 12
# Env-only root (startup validation)
MIN_ROOT_PASSWORD_LENGTH = 16
# Upper bounds shared by request bodies and root env validation
MAX_ADMIN_LOGIN_LENGTH = 128
MAX_ADMIN_PASSWORD_LENGTH = 1024


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


# Argon2 runs in worker threads under a dedicated limiter (2 at a time). Queued calls wait
# on the event loop, not inside shared pool threads. One limiter per loop (tests use many).
ARGON2_MAX_CONCURRENCY = 2
_ARGON2_LIMITERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
    weakref.WeakKeyDictionary()
)


def _argon2_limiter() -> anyio.CapacityLimiter:
    loop = asyncio.get_running_loop()
    limiter = _ARGON2_LIMITERS.get(loop)
    if limiter is None:
        limiter = anyio.CapacityLimiter(ARGON2_MAX_CONCURRENCY)
        _ARGON2_LIMITERS[loop] = limiter
    return limiter


async def run_argon2[T](fn: Callable[..., T], *args: object) -> T:
    return await anyio.to_thread.run_sync(fn, *args, limiter=_argon2_limiter())


async def averify_password(password_hash: str, password: str) -> bool:
    # Late-bound lookup keeps verify_password patchable in tests.
    return await run_argon2(lambda: verify_password(password_hash, password))


async def ahash_password(password: str) -> str:
    return await run_argon2(lambda: hash_password(password))
