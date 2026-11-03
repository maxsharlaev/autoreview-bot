"""Admin API: session auth, root recovery, users, read-only runs/queue."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.api.v1 import admin as admin_api
from app.config import Settings, get_settings
from app.models import AdminUser
from app.services.admin_config import AdminApiConfigError, validate_admin_env
from app.services.admin_passwords import hash_password, verify_password
from app.services.admin_sessions import (
    check_and_bump_rate_limit,
    create_session,
    destroy_session,
    invalidate_user_sessions,
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
ROOT_PASSWORD = "root-password-12"
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


class FakeRedis:
    """Minimal async Redis for session + rate-limit tests."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.expiry: dict[str, int] = {}
        self.sets: dict[str, set[str]] = {}

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
            if key in self.sets:
                del self.sets[key]
                n += 1
        return n

    async def incr(self, key: str) -> int:
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiry[key] = seconds
        return True

    async def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(members)
        return len(bucket) - before

    async def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key, set())
        n = 0
        for m in members:
            if m in bucket:
                bucket.remove(m)
                n += 1
        return n

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)


class FakeRedisPipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple] = []

    def delete(self, key: str) -> FakeRedisPipeline:
        self.ops.append(("delete", key))
        return self

    async def execute(self) -> list[Any]:
        out = []
        for op, key in self.ops:
            if op == "delete":
                out.append(await self.redis.delete(key))
        return out


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
    """In-memory AdminUser store with SQLAlchemy-ish execute/add/commit."""

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

    async def execute(self, stmt):  # noqa: ANN001
        sql = self._compiled(stmt)
        sql_l = sql.lower()
        if "admin_users" in sql_l or "AdminUser" in type(stmt).__mro__[0].__name__:
            if "count(" in sql_l and "admin_users" in sql_l:
                return FakeResult(scalar=len(self.users))
            # list all
            if "order by" in sql_l:
                return FakeResult(scalars_list=sorted(self.users.values(), key=lambda u: u.login))
            # match by login literal
            for login, user in self.by_login.items():
                if f"'{login}'" in sql or f'"{login}"' in sql:
                    return FakeResult(scalar=user, scalars_list=[user])
            for uid, user in self.users.items():
                uid_s = str(uid)
                if uid_s in sql or uid_s.replace("-", "") in sql:
                    return FakeResult(scalar=user, scalars_list=[user])
            # insert path / empty select
            if "where" in sql_l:
                return FakeResult(scalar=None)
            return FakeResult(scalars_list=list(self.users.values()))
        if "review_runs" in sql_l:
            if "count(" in sql_l:
                return FakeResult(scalar=self._count)
            if self._run is not None and "where" in sql_l and "join" not in sql_l.split("where")[0][-20:]:
                # detail by id
                if str(self._run.id) in sql or "where" in sql_l:
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
) -> AdminUser:
    return AdminUser(
        id=uuid.uuid4(),
        login=login,
        password_hash=hash_password(password),
        role=role,
        is_active=is_active,
        last_login_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class _SessionFactory:
    """async context manager factory returning the shared FakeSession."""

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
    application.include_router(admin_api.router, prefix="/api/v1/admin", tags=["admin"])
    application.state.session_factory = _SessionFactory(session)
    application.state.redis = redis

    async def session_override() -> AsyncIterator[FakeSession]:
        yield session

    from app.api.deps import get_session

    application.dependency_overrides[get_session] = session_override
    return application


def _client(session: FakeSession, redis: FakeRedis, settings: Settings | None = None) -> TestClient:
    settings = settings or _settings()
    application = _app(session, redis)
    # Note: caller must keep patches active while using the client, or rely on
    # TestClient context; we patch get_settings for the lifetime of construction
    # and return a client that still needs outer patches for request handlers.
    return TestClient(application)


# ---- env validation ---------------------------------------------------------


def test_validate_admin_both_or_neither() -> None:
    validate_admin_env(_settings(admin_root_login="", admin_root_password=""))
    with pytest.raises(AdminApiConfigError, match="both"):
        validate_admin_env(_settings(admin_root_login="root", admin_root_password=""))
    with pytest.raises(AdminApiConfigError, match="both"):
        validate_admin_env(_settings(admin_root_login="", admin_root_password=ROOT_PASSWORD))


def test_validate_admin_password_min_length() -> None:
    with pytest.raises(AdminApiConfigError, match="at least"):
        validate_admin_env(_settings(admin_root_password="short"))


def test_validate_admin_disabled_ignores_root_env() -> None:
    validate_admin_env(_settings(admin_api_enabled=False, admin_root_login="only", admin_root_password=""))
    validate_admin_env(_settings(admin_api_enabled=False, admin_root_login="x", admin_root_password="short"))


def test_password_hash_never_equals_plaintext() -> None:
    h = hash_password(ADMIN_PASSWORD)
    assert ADMIN_PASSWORD not in h
    assert verify_password(h, ADMIN_PASSWORD)
    assert not verify_password(h, "wrong-password")


# ---- sessions / rate limit unit --------------------------------------------


@pytest.mark.asyncio
async def test_session_create_load_destroy() -> None:
    redis = FakeRedis()
    sess = await create_session(redis, kind="root", role="admin", login=ROOT_LOGIN, user_id=None, ttl_seconds=60)
    loaded = await load_session(redis, sess.token)
    assert loaded is not None
    assert loaded.login == ROOT_LOGIN
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


# ---- HTTP API --------------------------------------------------------------


def test_disabled_not_mounted() -> None:
    application = FastAPI()
    # deliberately do not include admin router
    client = TestClient(application)
    assert client.get("/api/v1/admin/me").status_code == 404
    assert client.post("/api/v1/admin/login", json={"login": "x", "password": "y"}).status_code == 404


def test_root_login_works_without_db_users() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        bad = client.post(
            "/api/v1/admin/login",
            headers={"Content-Type": "application/json"},
            json={"login": ROOT_LOGIN, "password": "wrong-password-xx"},
        )
        assert bad.status_code == 401
        assert bad.json()["detail"] == "invalid credentials"
        ok = client.post(
            "/api/v1/admin/login",
            headers={"Content-Type": "application/json"},
            json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
        )
        assert ok.status_code == 200
        assert ok.json()["kind"] == "root"
        assert ok.json()["role"] == "admin"
        assert "opr_admin_session" in ok.cookies
        me = client.get("/api/v1/admin/me")
        assert me.status_code == 200
        assert me.json() == {"login": ROOT_LOGIN, "role": "admin", "kind": "root"}
        # root is not in DB
        assert session.users == {}


def test_root_login_works_with_db_users() -> None:
    session = FakeSession()
    session.seed_user(_make_user(login="alice", password=ADMIN_PASSWORD, role="admin"))
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
        assert ok.json()["kind"] == "root"
        assert ROOT_LOGIN not in session.by_login


def test_db_user_login_and_logout() -> None:
    session = FakeSession()
    user = _make_user(login="bob", password=VIEWER_PASSWORD, role="viewer")
    session.seed_user(user)
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
            json={"login": "bob", "password": VIEWER_PASSWORD},
        )
        assert ok.status_code == 200
        assert ok.json()["kind"] == "user"
        assert ok.json()["role"] == "viewer"
        token = ok.cookies.get("opr_admin_session")
        assert token
        assert any(token in k or redis.store.get(k) for k in redis.store)
        out = client.post("/api/v1/admin/logout")
        assert out.status_code == 200
        me = client.get("/api/v1/admin/me")
        assert me.status_code == 401


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
            headers={"Authorization": "Bearer operator-key-not-admin", "X-Api-Key": "operator-key-not-admin"},
        )
        assert resp.status_code == 401


def test_login_rate_limit() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings(admin_login_rate_limit_per_login=3, admin_login_rate_limit_per_ip=100)
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        for _ in range(3):
            r = client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": "nope", "password": "wrong-password"},
            )
            assert r.status_code == 401
        limited = client.post(
            "/api/v1/admin/login",
            headers={"Content-Type": "application/json"},
            json={"login": "nope", "password": "wrong-password"},
        )
        assert limited.status_code == 429


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
        login = client.post(
            "/api/v1/admin/login",
            headers={"Content-Type": "application/json"},
            json={"login": "view", "password": VIEWER_PASSWORD},
        )
        assert login.status_code == 200
        denied = client.post(
            "/api/v1/admin/users",
            headers={"Content-Type": "application/json"},
            json={"login": "new", "password": ADMIN_PASSWORD, "role": "viewer"},
        )
        assert denied.status_code == 403


def test_admin_create_user_and_no_hash_in_response() -> None:
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
        created = client.post(
            "/api/v1/admin/users",
            headers={"Content-Type": "application/json"},
            json={"login": "carol", "password": ADMIN_PASSWORD, "role": "admin"},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["login"] == "carol"
        assert "password" not in body
        assert "password_hash" not in body
        assert "argon2" not in created.text
        # conflict with root login
        clash = client.post(
            "/api/v1/admin/users",
            headers={"Content-Type": "application/json"},
            json={"login": ROOT_LOGIN, "password": ADMIN_PASSWORD, "role": "viewer"},
        )
        assert clash.status_code == 409


def test_deactivate_invalidates_sessions() -> None:
    session = FakeSession()
    user = _make_user(login="dave", password=ADMIN_PASSWORD, role="admin")
    session.seed_user(user)
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        # login as dave
        assert (
            client.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": "dave", "password": ADMIN_PASSWORD},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/admin/me").status_code == 200
        # root deactivates dave
        client2_session = session
        client2_redis = redis
        client2 = _client(client2_session, client2_redis, settings)
        assert (
            client2.post(
                "/api/v1/admin/login",
                headers={"Content-Type": "application/json"},
                json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
            ).status_code
            == 200
        )
        patched = client2.patch(
            f"/api/v1/admin/users/{user.id}",
            headers={"Content-Type": "application/json"},
            json={"is_active": False},
        )
        assert patched.status_code == 200
        assert patched.json()["is_active"] is False
        # dave's old cookie client should fail
        assert client.get("/api/v1/admin/me").status_code == 401


def test_runs_safe_fields_no_secrets() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    pr = SimpleNamespace(number=3, repository=SimpleNamespace(full_name="acme/demo"))
    run = SimpleNamespace(
        id=run_id,
        owner_id="acme",
        pull_request=pr,
        head_sha="d" * 40,
        status="completed",
        trigger="manual",
        created_at=now,
        updated_at=now,
        comment_url="https://example/c",
        has_sensitive_content=True,
        error_code=None,
        skip_reason=None,
        summary={"raw": "secret-model-output"},
        arq_job_id="hide-me",
    )
    session._runs = [run]
    session._count = 1
    session._run = run
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
        listed = client.get("/api/v1/admin/runs")
        assert listed.status_code == 200
        item = listed.json()["items"][0]
        assert item["id"] == str(run_id)
        assert item["repository"] == "acme/demo"
        assert "summary" not in item
        assert "arq_job_id" not in item
        assert "secret-model-output" not in listed.text


def test_queue_endpoint() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    snap = {
        "in_flight": {"pending": 0, "running": 1},
        "queue_jobs": 4,
        "stale_minutes": 15,
        "stale_runs": [],
        "last_completed": None,
        "last_failures": [],
        "hints": [],
    }
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.ops_snapshot", new=AsyncMock(return_value=snap)),
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
        resp = client.get("/api/v1/admin/queue")
        assert resp.status_code == 200
        assert resp.json()["queue_jobs"] == 4


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
            data=json.dumps({"login": ROOT_LOGIN, "password": ROOT_PASSWORD}),
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 415


@pytest.mark.asyncio
async def test_invalidate_user_sessions_removes_tokens() -> None:
    redis = FakeRedis()
    uid = str(uuid.uuid4())
    s1 = await create_session(redis, kind="user", role="admin", login="x", user_id=uid, ttl_seconds=60)
    s2 = await create_session(redis, kind="user", role="admin", login="x", user_id=uid, ttl_seconds=60)
    await invalidate_user_sessions(redis, uid)
    assert await load_session(redis, s1.token) is None
    assert await load_session(redis, s2.token) is None
