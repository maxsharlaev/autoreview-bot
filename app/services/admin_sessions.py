"""Redis-backed admin sessions and login rate limits."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis

AdminRole = Literal["admin", "viewer"]
AdminKind = Literal["root", "user"]

SESSION_KEY_PREFIX = "admin:session:"
USER_SESSIONS_PREFIX = "admin:user_sessions:"
RATE_LOGIN_PREFIX = "admin:rl:login:"
RATE_IP_PREFIX = "admin:rl:ip:"


@dataclass(frozen=True)
class AdminSession:
    token: str
    kind: AdminKind
    role: AdminRole
    login: str
    user_id: str | None  # None for root


def _session_key(token: str) -> str:
    return f"{SESSION_KEY_PREFIX}{token}"


def _user_sessions_key(user_id: str) -> str:
    return f"{USER_SESSIONS_PREFIX}{user_id}"


async def create_session(
    redis: Redis,
    *,
    kind: AdminKind,
    role: AdminRole,
    login: str,
    user_id: str | None,
    ttl_seconds: int,
) -> AdminSession:
    token = secrets.token_urlsafe(32)
    payload = {
        "kind": kind,
        "role": role,
        "login": login,
        "user_id": user_id,
    }
    await redis.set(_session_key(token), json.dumps(payload), ex=ttl_seconds)
    if user_id:
        await redis.sadd(_user_sessions_key(user_id), token)
        await redis.expire(_user_sessions_key(user_id), ttl_seconds)
    return AdminSession(token=token, kind=kind, role=role, login=login, user_id=user_id)


async def load_session(redis: Redis, token: str | None) -> AdminSession | None:
    if not token:
        return None
    raw = await redis.get(_session_key(token))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    kind = data.get("kind")
    role = data.get("role")
    login = data.get("login")
    if kind not in ("root", "user") or role not in ("admin", "viewer") or not login:
        return None
    user_id = data.get("user_id")
    return AdminSession(token=token, kind=kind, role=role, login=login, user_id=user_id)


async def destroy_session(redis: Redis, token: str | None) -> None:
    if not token:
        return
    session = await load_session(redis, token)
    await redis.delete(_session_key(token))
    if session and session.user_id:
        await redis.srem(_user_sessions_key(session.user_id), token)


async def invalidate_user_sessions(redis: Redis, user_id: str) -> None:
    key = _user_sessions_key(user_id)
    tokens = await redis.smembers(key)
    if tokens:
        pipe = redis.pipeline()
        for token in tokens:
            if isinstance(token, bytes):
                token = token.decode("utf-8")
            pipe.delete(_session_key(token))
        pipe.delete(key)
        await pipe.execute()
    else:
        await redis.delete(key)


async def check_and_bump_rate_limit(
    redis: Redis,
    *,
    login: str,
    ip: str,
    per_login: int,
    per_ip: int,
    window_seconds: int,
) -> bool:
    """Return True if the attempt is allowed; bump counters either way when under limit.

    Returns False when limited (caller should not reveal which bucket tripped).
    """
    login_key = f"{RATE_LOGIN_PREFIX}{(login or '').strip().lower() or '_'}"
    ip_key = f"{RATE_IP_PREFIX}{(ip or '').strip() or '_'}"

    login_count = int(await redis.incr(login_key))
    if login_count == 1:
        await redis.expire(login_key, window_seconds)
    ip_count = int(await redis.incr(ip_key))
    if ip_count == 1:
        await redis.expire(ip_key, window_seconds)

    if login_count > per_login or ip_count > per_ip:
        return False
    return True
