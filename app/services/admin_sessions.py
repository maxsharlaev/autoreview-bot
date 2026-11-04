"""Redis-backed admin sessions and login rate limits."""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis

# Re-export for callers / tests
from app.services.admin_root_crypto import (  # noqa: F401
    compute_root_fingerprint,
)

AdminRole = Literal["admin", "viewer"]
AdminKind = Literal["root", "user"]

SESSION_KEY_PREFIX = "admin:session:"
RATE_LOGIN_PREFIX = "admin:rl:login:"
RATE_IP_PREFIX = "admin:rl:ip:"


@dataclass(frozen=True)
class AdminSession:
    token: str
    kind: AdminKind
    role: AdminRole
    login: str
    user_id: str | None  # None for root
    session_version: int | None  # DB users only
    root_fingerprint: str | None  # root only


def _session_key(token: str) -> str:
    return f"{SESSION_KEY_PREFIX}{token}"


async def create_session(
    redis: Redis,
    *,
    kind: AdminKind,
    role: AdminRole,
    login: str,
    user_id: str | None,
    ttl_seconds: int,
    session_version: int | None = None,
    root_fingerprint: str | None = None,
) -> AdminSession:
    token = secrets.token_urlsafe(32)
    payload: dict = {
        "kind": kind,
        "role": role,
        "login": login,
        "user_id": user_id,
        "session_version": session_version,
        "root_fingerprint": root_fingerprint,
    }
    await redis.set(_session_key(token), json.dumps(payload), ex=ttl_seconds)
    return AdminSession(
        token=token,
        kind=kind,
        role=role,
        login=login,
        user_id=user_id,
        session_version=session_version,
        root_fingerprint=root_fingerprint,
    )


def _parse_session(raw: str | bytes) -> tuple | None:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    role = data.get("role")
    login = data.get("login")
    user_id = data.get("user_id")
    version = data.get("session_version")
    fingerprint = data.get("root_fingerprint")
    if kind not in ("root", "user") or role not in ("admin", "viewer") or not isinstance(login, str) or not login:
        return None
    if user_id is not None:
        try:
            user_id = str(uuid.UUID(user_id))
        except (TypeError, ValueError, AttributeError):
            return None
    if version is not None and not isinstance(version, int):
        try:
            version = int(version)
        except (TypeError, ValueError):
            return None
    return kind, role, login, user_id, version, fingerprint if isinstance(fingerprint, str) else None


async def load_session(redis: Redis, token: str | None) -> AdminSession | None:
    if not token:
        return None
    raw = await redis.get(_session_key(token))
    if not raw:
        return None
    data = _parse_session(raw)
    if data is None:
        # Malformed stored session: drop it so the request ends as a plain 401.
        await redis.delete(_session_key(token))
        return None
    kind, role, login, user_id, version, fingerprint = data
    return AdminSession(
        token=token,
        kind=kind,
        role=role,
        login=login,
        user_id=user_id,
        session_version=version,
        root_fingerprint=fingerprint,
    )


async def destroy_session(redis: Redis, token: str | None) -> None:
    if not token:
        return
    await redis.delete(_session_key(token))


# INCR + EXPIRE atomically; a key left without TTL (e.g. crash between calls) gets one again.
_INCR_WITH_TTL_LUA = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def _incr_with_ttl(redis: Redis, key: str, window_seconds: int) -> int:
    return int(await redis.eval(_INCR_WITH_TTL_LUA, 1, key, int(window_seconds)))


async def check_and_bump_rate_limit(
    redis: Redis,
    *,
    login: str,
    ip: str,
    per_login: int,
    per_ip: int,
    window_seconds: int,
) -> bool:
    """Return True if the attempt is allowed; bump counters either way when under limit."""
    login_key = f"{RATE_LOGIN_PREFIX}{(login or '').strip().lower() or '_'}"
    ip_key = f"{RATE_IP_PREFIX}{(ip or '').strip() or '_'}"

    login_count = await _incr_with_ttl(redis, login_key, window_seconds)
    ip_count = await _incr_with_ttl(redis, ip_key, window_seconds)

    if login_count > per_login or ip_count > per_ip:
        return False
    return True
