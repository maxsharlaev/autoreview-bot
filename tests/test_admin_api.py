"""Admin API: session auth, credential versioning, rate limit IP, JSON bodies."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.api.v1 import admin as admin_api
from app.config import Settings, get_settings
from app.models import AdminUser
from app.services.admin_config import AdminApiConfigError, validate_admin_env
from app.services.admin_passwords import hash_password, verify_password
from app.services.admin_sessions import (
    check_and_bump_rate_limit,
    compute_root_fingerprint,
    create_session,
    destroy_session,
    load_session,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    _clear_settings_cache()
    yield
    _clear_settings_cache()


ROOT_LOGIN = "rootadmin"
ROOT_PASSWORD = "root-password-16c"
VIEWER_PASSWORD = "viewer-pass-12"
ADMIN_PASSWORD = "admin-pass-123"


def _settings(**kwargs: Any) -> Settings:
    base = {
        "admin_api_enabled": True,
        "admin_root_login": ROOT_LOGIN,
        "admin_root_password": ROOT_PASSWORD,
        "admin_session_ttl_seconds": 3600,
        "admin_cookie_secure": False,
        "admin_login_rate_limit_per_login": 5,
        "admin_login_rate_limit_per_ip": 20,
        "admin_login_rate_window_seconds": 300,
        "review_api_key": "operator-key-not-admin",
    }
    base.update(kwargs)
    return Settings(**base)


@pytest.fixture(scope="module")
def _root_creds():  # noqa: ANN202
    from app.services import admin_root_crypto

    creds = admin_root_crypto.load_root_credentials(ROOT_LOGIN, ROOT_PASSWORD)
    yield creds
    admin_root_crypto.clear_root_credentials()


@pytest.fixture(autouse=True)
def _startup_root(_root_creds, monkeypatch):  # noqa: ANN001, ANN202
    # Mirrors warm_admin_root_caches at startup for the default _settings() root.
    from app.services import admin_root_crypto

    monkeypatch.setattr(admin_root_crypto, "_root", _root_creds)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.expiry: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.expiry[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                n += 1
        return n

    async def incr(self, key: str) -> int:
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiry[key] = seconds
        return True

    async def eval(self, script: str, numkeys: int, key: str, window: int) -> int:
        # Mirrors _INCR_WITH_TTL_LUA: INCR, then EXPIRE when the key has no TTL.
        assert numkeys == 1 and "INCR" in script and "TTL" in script
        val = await self.incr(key)
        if key not in self.expiry:
            self.expiry[key] = int(window)
        return val


class FakeResult:
    def __init__(self, *, scalar=None, scalars_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars_list))


class FakeSession:
    def __init__(self) -> None:
        self.users: dict[uuid.UUID, AdminUser] = {}
        self.by_login: dict[str, AdminUser] = {}
        self._run: Any = None
        self._runs: list[Any] = []
        self._count = 0

    def add(self, obj: AdminUser) -> None:
        self.users[obj.id] = obj
        self.by_login[obj.login] = obj

    async def commit(self) -> None:
        return None

    async def refresh(self, obj: AdminUser) -> None:
        return None

    def _compiled(self, stmt) -> str:  # noqa: ANN001
        try:
            return str(stmt.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            return str(stmt)

    async def execute(self, stmt, *args, **kwargs):  # noqa: ANN001
        sql = self._compiled(stmt)
        sql_l = sql.lower()
        if sql_l.strip().startswith("update") and "admin_users" in sql_l:
            # Apply password_hash / role / is_active / session_version bump for unit tests.
            target = None
            for uid, user in self.users.items():
                uid_s = str(uid)
                if uid_s in sql or uid_s.replace("-", "") in sql:
                    target = user
                    break
            if target is None:
                return FakeResult(scalar=None)
            if "password_hash" in sql_l:
                # cannot parse hash from SQL easily; leave existing or set marker
                pass
            if "role" in sql_l and "viewer" in sql:
                target.role = "viewer"
            if "role" in sql_l and "'admin'" in sql:
                target.role = "admin"
            if "is_active" in sql_l and "false" in sql_l:
                target.is_active = False
            if "is_active" in sql_l and "true" in sql_l and "false" not in sql.split("is_active")[-1][:40].lower():
                # crude
                if "true" in sql_l:
                    target.is_active = True
            if "session_version" in sql_l:
                target.session_version = int(target.session_version) + 1
            # password_hash from bound params - try literal
            if "password_hash" in sql_l:
                # unit tests call hash_password before update; store via side channel if present
                if getattr(self, "_pending_password_hash", None):
                    target.password_hash = self._pending_password_hash
                    self._pending_password_hash = None
            self.by_login[target.login] = target
            return FakeResult(scalar=target, scalars_list=[target])
        if "admin_users" in sql_l:
            if "count(" in sql_l and "admin_users" in sql_l:
                return FakeResult(scalar=len(self.users))
            if "order by" in sql_l:
                return FakeResult(scalars_list=sorted(self.users.values(), key=lambda u: u.login))
            for login, user in self.by_login.items():
                if f"'{login}'" in sql or f'"{login}"' in sql:
                    return FakeResult(scalar=user, scalars_list=[user])
            for uid, user in self.users.items():
                uid_s = str(uid)
                if uid_s in sql or uid_s.replace("-", "") in sql:
                    return FakeResult(scalar=user, scalars_list=[user])
            if "where" in sql_l:
                return FakeResult(scalar=None)
            return FakeResult(scalars_list=list(self.users.values()))
        if "review_runs" in sql_l:
            if "count(" in sql_l:
                return FakeResult(scalar=self._count)
            if self._run is not None and "where" in sql_l:
                return FakeResult(scalar=self._run)
            return FakeResult(scalars_list=self._runs, scalar=self._count)
        return FakeResult()

    def seed_user(self, user: AdminUser) -> None:
        self.add(user)


def _make_user(
    *,
    login: str,
    password: str,
    role: str = "viewer",
    is_active: bool = True,
    session_version: int = 0,
) -> AdminUser:
    return AdminUser(
        id=uuid.uuid4(),
        login=login,
        password_hash=hash_password(password),
        role=role,
        is_active=is_active,
        session_version=session_version,
        last_login_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class _SessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _app(session: FakeSession, redis: FakeRedis) -> FastAPI:
    application = FastAPI()
    admin_api.mount_admin_api(application)
    application.state.session_factory = _SessionFactory(session)
    application.state.redis = redis

    async def session_override() -> AsyncIterator[FakeSession]:
        yield session

    from app.api.deps import get_session

    application.dependency_overrides[get_session] = session_override
    return application


def _client(session: FakeSession, redis: FakeRedis, settings: Settings | None = None) -> TestClient:
    return TestClient(_app(session, redis))


# ---- env validation ---------------------------------------------------------


def test_validate_admin_both_or_neither() -> None:
    validate_admin_env(_settings(admin_root_login="", admin_root_password=""))
    with pytest.raises(AdminApiConfigError, match="both"):
        validate_admin_env(_settings(admin_api_enabled=True, admin_root_login="root", admin_root_password=""))
    with pytest.raises(AdminApiConfigError, match="both"):
        validate_admin_env(_settings(admin_api_enabled=True, admin_root_login="", admin_root_password=ROOT_PASSWORD))


def test_validate_admin_password_min_length() -> None:
    with pytest.raises(AdminApiConfigError, match="at least"):
        validate_admin_env(_settings(admin_root_password="short-12chars"))


def test_validate_admin_disabled_ignores_root_env() -> None:
    validate_admin_env(_settings(admin_api_enabled=False, admin_root_login="only", admin_root_password=""))
    validate_admin_env(_settings(admin_api_enabled=False, admin_root_login="x", admin_root_password="short"))


def test_password_hash_never_equals_plaintext() -> None:
    h = hash_password(ADMIN_PASSWORD)
    assert ADMIN_PASSWORD not in h
    assert verify_password(h, ADMIN_PASSWORD)


# ---- sessions unit ----------------------------------------------------------


@pytest.mark.asyncio
async def test_session_create_load_destroy() -> None:
    redis = FakeRedis()
    fp = compute_root_fingerprint(ROOT_LOGIN, ROOT_PASSWORD)
    sess = await create_session(
        redis,
        kind="root",
        role="admin",
        login=ROOT_LOGIN,
        user_id=None,
        ttl_seconds=60,
        root_fingerprint=fp,
    )
    loaded = await load_session(redis, sess.token)
    assert loaded is not None
    assert loaded.root_fingerprint == fp
    await destroy_session(redis, sess.token)
    assert await load_session(redis, sess.token) is None


@pytest.mark.asyncio
async def test_rate_limit_per_login() -> None:
    redis = FakeRedis()
    for _ in range(3):
        assert await check_and_bump_rate_limit(
            redis, login="a", ip="1.1.1.1", per_login=3, per_ip=100, window_seconds=60
        )
    assert not await check_and_bump_rate_limit(
        redis, login="a", ip="1.1.1.1", per_login=3, per_ip=100, window_seconds=60
    )


# ---- HTTP -------------------------------------------------------------------


def test_disabled_not_mounted() -> None:
    application = FastAPI()
    client = TestClient(application)
    assert client.get("/api/v1/admin/me").status_code == 404


def test_root_login_and_me() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        ok = client.post(
            "/api/v1/admin/login",
            headers={"Content-Type": "application/json"},
            json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
        )
        assert ok.status_code == 200
        me = client.get("/api/v1/admin/me")
        assert me.status_code == 200
        assert me.json()["kind"] == "root"


def test_root_session_rejected_after_password_change() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/admin/me").status_code == 200
        # Rotate root password in .env + restart (startup recomputes the fingerprint).
        from app.services.admin_root_crypto import load_root_credentials

        load_root_credentials(ROOT_LOGIN, "root-password-rotated")
        assert client.get("/api/v1/admin/me").status_code == 401


def test_root_session_rejected_after_root_removed() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
            ).status_code
            == 200
        )
        # Root removed from .env + restart: startup leaves no root credentials.
        from app.services.admin_root_crypto import clear_root_credentials

        clear_root_credentials()
        assert client.get("/api/v1/admin/me").status_code == 401


def test_password_reset_rejects_old_and_stale_version_session() -> None:
    session = FakeSession()
    user = _make_user(login="dave", password=ADMIN_PASSWORD, role="admin", session_version=0)
    session.seed_user(user)
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": "dave", "password": ADMIN_PASSWORD},
            ).status_code
            == 200
        )
        token_v0 = client.cookies.get("opr_admin_session")
        assert token_v0
        # Simulate a race: another session minted at the old version still in Redis
        # while reset bumps the DB version.
        import asyncio

        stale = asyncio.run(
            create_session(
                redis,
                kind="user",
                role="admin",
                login="dave",
                user_id=str(user.id),
                ttl_seconds=60,
                session_version=0,
            )
        )
        # Root resets password → version 1
        root = _client(session, redis, settings)
        assert (
            root.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
            ).status_code
            == 200
        )
        reset = root.post(
            f"/api/v1/admin/users/{user.id}/reset-password",
            headers={"Content-Type": "application/json"},
            json={"password": "brand-new-pass1"},
        )
        assert reset.status_code == 200
        assert user.session_version == 1
        # Original dave cookie (v0) rejected
        assert client.get("/api/v1/admin/me").status_code == 401
        # Explicitly stale v0 session also rejected
        redis_token = stale.token
        # put cookie on a fresh client
        c2 = _client(session, redis, settings)
        c2.cookies.set("opr_admin_session", redis_token)
        assert c2.get("/api/v1/admin/me").status_code == 401


def test_role_change_and_deactivate_bump_version() -> None:
    session = FakeSession()
    user = _make_user(login="erin", password=ADMIN_PASSWORD, role="admin", session_version=0)
    session.seed_user(user)
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": "erin", "password": ADMIN_PASSWORD},
            ).status_code
            == 200
        )
        root = _client(session, redis, settings)
        assert (
            root.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
            ).status_code
            == 200
        )
        assert (
            root.patch(
                f"/api/v1/admin/users/{user.id}",
                headers={"Content-Type": "application/json"},
                json={"role": "viewer"},
            ).status_code
            == 200
        )
        assert user.session_version == 1
        assert client.get("/api/v1/admin/me").status_code == 401

        # re-login as viewer then deactivate
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": "erin", "password": ADMIN_PASSWORD},
            ).status_code
            == 200
        )
        assert (
            root.patch(
                f"/api/v1/admin/users/{user.id}",
                headers={"Content-Type": "application/json"},
                json={"is_active": False},
            ).status_code
            == 200
        )
        assert user.session_version == 2
        assert client.get("/api/v1/admin/me").status_code == 401


def test_xff_spoof_does_not_change_ip_bucket() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings(admin_login_rate_limit_per_login=100, admin_login_rate_limit_per_ip=3)
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        for i in range(3):
            r = client.post(
                "/api/v1/admin/login",
                headers={
                    "Content-Type": "application/json",
                    "X-Forwarded-For": f"203.0.113.{i}",
                },
                json={"login": f"nope{i}", "password": "wrong-password"},
            )
            assert r.status_code == 401
        limited = client.post(
            "/api/v1/admin/login",
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-For": "198.51.100.1",
            },
            json={"login": "other", "password": "wrong-password"},
        )
        assert limited.status_code == 429
        # Confirm the IP bucket key is the TestClient peer, not XFF
        assert any(k.startswith("admin:rl:ip:testclient") for k in redis.store)


@pytest.mark.parametrize(
    ("path", "method", "payload"),
    [
        ("/api/v1/admin/login", "post", None),
        ("/api/v1/admin/users", "post", None),
        ("/api/v1/admin/users/{uid}", "patch", None),
        ("/api/v1/admin/users/{uid}/reset-password", "post", None),
    ],
)
def test_malformed_json_returns_422(path: str, method: str, payload: None) -> None:
    session = FakeSession()
    user = _make_user(login="admin1", password=ADMIN_PASSWORD, role="admin")
    session.seed_user(user)
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        if "{uid}" in path:
            assert (
                client.post(
                    "/api/v1/admin/login",
                    headers={"Content-Type": "application/json"},
                    json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
                ).status_code
                == 200
            )
            path = path.replace("{uid}", str(user.id))
        resp = getattr(client, method)(
            path,
            content="{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422


def test_viewer_cannot_write_users() -> None:
    session = FakeSession()
    viewer = _make_user(login="view", password=VIEWER_PASSWORD, role="viewer")
    session.seed_user(viewer)
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": "view", "password": VIEWER_PASSWORD},
            ).status_code
            == 200
        )
        denied = client.post(
            "/api/v1/admin/users",
            headers={"Content-Type": "application/json"},
            json={"login": "new", "password": ADMIN_PASSWORD, "role": "viewer"},
        )
        assert denied.status_code == 403


def test_login_requires_json_content_type() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        resp = client.post(
            "/api/v1/admin/login",
            content=json.dumps({"login": ROOT_LOGIN, "password": ROOT_PASSWORD}),
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 415


def test_review_api_key_does_not_grant_admin() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        resp = client.get(
            "/api/v1/admin/me",
            headers={"Authorization": "Bearer operator-key-not-admin"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rate_limit_repairs_key_without_ttl() -> None:
    from app.services.admin_sessions import RATE_LOGIN_PREFIX, check_and_bump_rate_limit

    redis = FakeRedis()
    stuck = f"{RATE_LOGIN_PREFIX}stuck"
    redis.store[stuck] = "3"  # no TTL: would never expire with the old count==1 logic
    allowed = await check_and_bump_rate_limit(
        redis, login="stuck", ip="1.2.3.4", per_login=5, per_ip=20, window_seconds=300
    )
    assert allowed
    assert redis.store[stuck] == "4"
    assert redis.expiry[stuck] == 300


@pytest.mark.parametrize(
    ("field", "upper"),
    [
        ("admin_session_ttl_seconds", 604800),
        ("admin_login_rate_limit_per_login", 1000),
        ("admin_login_rate_limit_per_ip", 10000),
        ("admin_login_rate_window_seconds", 86400),
    ],
)
def test_admin_numeric_settings_bounds(field: str, upper: int) -> None:
    from pydantic import ValidationError

    assert getattr(_settings(**{field: 1}), field) == 1
    assert getattr(_settings(**{field: upper}), field) == upper
    for bad in (0, -1, upper + 1):
        with pytest.raises(ValidationError):
            _settings(**{field: bad})


def test_admin_responses_are_no_store_including_errors() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        responses = [
            client.get("/api/v1/admin/me"),  # 401
            client.post("/api/v1/admin/login", content="x", headers={"Content-Type": "text/plain"}),  # 415
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
            ),  # 200
            client.get("/api/v1/admin/me"),  # 200
            client.get("/api/v1/admin/nope"),  # 404
        ]
    assert [r.status_code for r in responses] == [401, 415, 200, 200, 404]
    for r in responses:
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["pragma"] == "no-cache"


def test_admin_queue_reports_global_depth_and_ops_snapshot_unchanged() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    snap = {"in_flight": {"pending": 0, "running": 0}, "queue_jobs": 7, "stale_minutes": 30}
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.ops_snapshot", AsyncMock(return_value=dict(snap))),
    ):
        client = _client(session, redis, settings)
        client.post(
            "/api/v1/admin/login",
            headers={"Content-Type": "application/json"},
            json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
        )
        body = client.get("/api/v1/admin/queue").json()
    assert body["queue_jobs_global"] == 7
    assert "queue_jobs" not in body


@pytest.mark.parametrize(
    ("login", "password", "status"),
    [
        (ROOT_LOGIN, "wrong-root-password", 401),
        ("erin", "wrong-password-x", 401),
        ("nobody-here", "whatever-password", 401),
        (ROOT_LOGIN, ROOT_PASSWORD, 200),
        ("erin", ADMIN_PASSWORD, 200),
    ],
)
def test_login_does_exactly_one_argon2_verify(login: str, password: str, status: int) -> None:
    from app.services import admin_passwords

    session = FakeSession()
    session.seed_user(_make_user(login="erin", password=ADMIN_PASSWORD, role="admin"))
    redis = FakeRedis()
    settings = _settings()
    import anyio.to_thread

    spy = MagicMock(side_effect=admin_passwords.verify_password)
    argon2_dispatches: list[object] = []
    real_run_sync = anyio.to_thread.run_sync

    async def counting_run_sync(fn, *args, **kwargs):  # noqa: ANN001, ANN202
        if kwargs.get("limiter") is not None:  # argon2 pool (sync FastAPI deps use the default pool)
            argon2_dispatches.append(fn)
        return await real_run_sync(fn, *args, **kwargs)

    lookups: list[str] = []
    real_execute = session.execute

    async def counting_execute(stmt, *args, **kwargs):  # noqa: ANN001, ANN202
        sql = session._compiled(stmt).lower()
        if sql.lstrip().startswith("select") and "admin_users" in sql:
            lookups.append(sql)
        return await real_execute(stmt, *args, **kwargs)

    session.execute = counting_execute  # type: ignore[method-assign]
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.services.admin_passwords.verify_password", spy),
        patch("anyio.to_thread.run_sync", counting_run_sync),
    ):
        client = _client(session, redis, settings)
        r = client.post(
            "/api/v1/admin/login",
            headers={"Content-Type": "application/json"},
            json={"login": login, "password": password},
        )
    assert r.status_code == status
    assert spy.call_count == 1
    assert len(lookups) == 1
    assert len(argon2_dispatches) == 1


def test_root_credentials_computed_once_and_not_rehashed() -> None:
    from app.services import admin_passwords, admin_root_crypto

    creds = admin_root_crypto.get_root_credentials()
    assert creds is not None and creds.login == ROOT_LOGIN
    assert admin_passwords.verify_password(creds.password_hash, ROOT_PASSWORD)
    # Settings changes without restart do not touch the startup snapshot.
    validate_admin_env(_settings(admin_root_password="another-root-pass-16"))
    assert admin_root_crypto.get_root_credentials() is creds
    validate_admin_env(_settings(admin_root_login="", admin_root_password=""))
    assert admin_root_crypto.get_root_credentials() is None


def test_root_env_max_lengths() -> None:
    from app.services.admin_passwords import MAX_ADMIN_LOGIN_LENGTH, MAX_ADMIN_PASSWORD_LENGTH

    validate_admin_env(_settings(admin_root_login="l" * MAX_ADMIN_LOGIN_LENGTH))
    validate_admin_env(_settings(admin_root_password="p" * MAX_ADMIN_PASSWORD_LENGTH))
    with pytest.raises(AdminApiConfigError, match="ADMIN_ROOT_LOGIN must be at most"):
        validate_admin_env(_settings(admin_root_login="l" * (MAX_ADMIN_LOGIN_LENGTH + 1)))
    with pytest.raises(AdminApiConfigError, match="ADMIN_ROOT_PASSWORD must be at most"):
        validate_admin_env(_settings(admin_root_password="p" * (MAX_ADMIN_PASSWORD_LENGTH + 1)))
    validate_admin_env(_settings())


def test_login_body_max_lengths() -> None:
    from app.api.v1.admin_schemas import AdminLoginRequest
    from app.services.admin_passwords import MAX_ADMIN_LOGIN_LENGTH, MAX_ADMIN_PASSWORD_LENGTH
    from pydantic import ValidationError

    AdminLoginRequest(login="l" * MAX_ADMIN_LOGIN_LENGTH, password="p" * MAX_ADMIN_PASSWORD_LENGTH)
    with pytest.raises(ValidationError):
        AdminLoginRequest(login="l" * (MAX_ADMIN_LOGIN_LENGTH + 1), password="p")
    with pytest.raises(ValidationError):
        AdminLoginRequest(login="l", password="p" * (MAX_ADMIN_PASSWORD_LENGTH + 1))


@pytest.mark.asyncio
async def test_slow_password_verify_does_not_block_other_requests() -> None:
    import asyncio
    import time

    import httpx
    from app.services import admin_passwords

    real_verify = admin_passwords.verify_password

    def slow_verify(password_hash: str, password: str) -> bool:
        time.sleep(0.5)
        return real_verify(password_hash, password)

    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    app = _app(session, redis)
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.services.admin_passwords.verify_password", slow_verify),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = asyncio.create_task(
                client.post("/api/v1/admin/login", json={"login": "nobody", "password": "whatever-pass"})
            )
            await asyncio.sleep(0.1)
            me = await client.get("/api/v1/admin/me")
            assert me.status_code == 401
            assert not login.done()
            assert (await login).status_code == 401


@pytest.mark.asyncio
async def test_saturated_argon2_does_not_starve_sync_dependencies() -> None:
    import asyncio
    import time

    import anyio.to_thread
    import httpx
    from app.services import admin_passwords

    real_verify = admin_passwords.verify_password

    def slow_verify(password_hash: str, password: str) -> bool:
        time.sleep(0.6)
        return real_verify(password_hash, password)

    session = FakeSession()
    redis = FakeRedis()
    settings = _settings(admin_login_rate_limit_per_ip=1000, admin_login_rate_limit_per_login=1000)
    app = _app(session, redis)
    pool = anyio.to_thread.current_default_thread_limiter()
    saved = pool.total_tokens
    pool.total_tokens = 4  # small shared pool: 8 queued logins would exhaust it if they held threads
    try:
        with (
            patch("app.api.admin_auth.get_settings", return_value=settings),
            patch("app.api.v1.admin.get_settings", return_value=settings),
            patch("app.services.admin_passwords.verify_password", slow_verify),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                logins = [
                    asyncio.create_task(
                        client.post("/api/v1/admin/login", json={"login": f"u{i}", "password": "whatever-pass"})
                    )
                    for i in range(8)
                ]
                await asyncio.sleep(0.1)
                started = time.monotonic()
                # ensure_json_content_type is a sync dependency -> runs in the shared pool.
                probe = await client.post("/api/v1/admin/login", content="x", headers={"Content-Type": "text/plain"})
                elapsed = time.monotonic() - started
                assert probe.status_code == 415
                assert elapsed < 0.4
                assert sum(t.done() for t in logins) == 0
                assert all(r.status_code == 401 for r in await asyncio.gather(*logins))
    finally:
        pool.total_tokens = saved


def test_logout_requires_json_content_type() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
            ).status_code
            == 200
        )
        form = client.post("/api/v1/admin/logout", data={"x": "1"})
        assert form.status_code == 415
        assert client.get("/api/v1/admin/me").status_code == 200
        assert client.post("/api/v1/admin/logout", json={}).status_code == 200
        assert client.get("/api/v1/admin/me").status_code == 401


def test_every_mutating_admin_route_requires_json() -> None:
    from app.api.v1.admin import ensure_json_content_type, router
    from fastapi.routing import APIRoute

    def deps(route: APIRoute) -> set:
        found, stack = set(), list(route.dependant.dependencies)
        while stack:
            d = stack.pop()
            found.add(d.call)
            stack.extend(d.dependencies)
        return found

    mutating = [r for r in router.routes if isinstance(r, APIRoute) and r.methods & {"POST", "PUT", "PATCH", "DELETE"}]
    assert mutating
    for route in mutating:
        assert ensure_json_content_type in deps(route), route.path


@pytest.mark.parametrize(
    "payload",
    [
        "42",  # JSON scalar
        json.dumps({"kind": "user", "role": "admin", "login": "erin", "user_id": "not-a-uuid", "session_version": 0}),
    ],
)
def test_malformed_stored_session_is_401_and_deleted(payload: str) -> None:
    from app.services.admin_sessions import SESSION_KEY_PREFIX

    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    key = f"{SESSION_KEY_PREFIX}bad-token"
    redis.store[key] = payload
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        client.cookies.set("opr_admin_session", "bad-token")
        assert client.get("/api/v1/admin/me").status_code == 401
    assert key not in redis.store
