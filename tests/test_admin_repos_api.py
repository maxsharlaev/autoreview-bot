"""Admin API: spaces (installations) and repos endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.api.v1 import admin as admin_api
from app.config import Settings, get_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_admin_api import (
    ROOT_LOGIN,
    ROOT_PASSWORD,
    FakeRedis,
    FakeSession,
    _settings,
)


def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    _clear_settings_cache()
    yield
    _clear_settings_cache()


@pytest.fixture(scope="module")
def _root_creds():  # noqa: ANN202
    from app.services import admin_root_crypto

    creds = admin_root_crypto.load_root_credentials(ROOT_LOGIN, ROOT_PASSWORD)
    yield creds
    admin_root_crypto.clear_root_credentials()


@pytest.fixture(autouse=True)
def _startup_root(_root_creds, monkeypatch):  # noqa: ANN001, ANN202
    from app.services import admin_root_crypto

    monkeypatch.setattr(admin_root_crypto, "_root", _root_creds)


def _app(session: FakeSession, redis: FakeRedis) -> FastAPI:
    application = FastAPI()
    admin_api.mount_admin_api(application)
    application.state.session_factory = lambda: SimpleNamespace(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=None),
    )
    application.state.redis = redis

    async def session_override() -> AsyncIterator[FakeSession]:
        yield session

    from app.api.deps import get_session

    application.dependency_overrides[get_session] = session_override
    return application


def _client(session: FakeSession, redis: FakeRedis, settings: Settings | None = None) -> TestClient:
    return TestClient(_app(session, redis))


def _login_as_root(client: TestClient) -> None:
    r = client.post(
        "/api/v1/admin/login",
        headers={"Content-Type": "application/json"},
        json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD},
    )
    assert r.status_code == 200


class FakeOwnerContext:
    def __init__(self, owner_id: str = "default") -> None:
        self.id = owner_id
        self.github = SimpleNamespace(
            kind="app",
            app_id=12345,
            private_key_pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
            installation_id=0,
            token=None,
        )
        self.is_default = True


class FakeOwnerRegistry:
    def __init__(self, owners: dict[str, Any] | None = None) -> None:
        self.owners = owners or {"default": FakeOwnerContext()}
        self._default = next(iter(self.owners.values()), None)

    def get(self, owner_id: str) -> Any:
        return self.owners.get(owner_id)

    def get_default(self) -> Any:
        return self._default

    def resolve(self, full_name: str, installation_id: int | None = None, explicit_owner: str | None = None) -> Any:
        return SimpleNamespace(owner_id="default", reason="default_fallback", rejected=lambda: False)


class FakeGitHubClient:
    def __init__(
        self,
        *,
        installations: list[dict[str, Any]] | None = None,
        repos: list[dict[str, Any]] | None = None,
        open_prs: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.installations = installations or []
        self.repos = repos or []
        self.open_prs = open_prs or []
        self.error = error
        self.calls: list[str] = []

    async def list_app_installations(self) -> list[dict[str, Any]]:
        self.calls.append("list_app_installations")
        if self.error:
            raise self.error
        return self.installations

    async def list_installation_repos(self, installation_id: int) -> list[dict[str, Any]]:
        self.calls.append(f"list_installation_repos:{installation_id}")
        if self.error:
            raise self.error
        return self.repos

    async def list_open_pulls(self, owner: str, repo: str) -> list[dict[str, Any]]:
        self.calls.append(f"list_open_pulls:{owner}/{repo}")
        if self.error:
            raise self.error
        return self.open_prs


def test_spaces_requires_auth() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 401


def test_spaces_lists_installations() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations = [
        {"id": 1001, "account": {"login": "acme-corp", "type": "Organization"}},
        {"id": 1002, "account": {"login": "user123", "type": "User"}},
    ]
    github = FakeGitHubClient(installations=installations)
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._github_for_default_owner", return_value=github),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["items"][0]["id"] == 1001
        assert data["items"][0]["login"] == "acme-corp"
        assert data["items"][0]["type"] == "Organization"
        assert data["items"][1]["login"] == "user123"
        assert data["items"][1]["type"] == "User"


def test_spaces_no_owner_configured() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin._github_for_default_owner", return_value=None),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 503
        assert "no owner configured" in resp.json()["detail"]


def test_spaces_github_auth_error() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    from app.adapters.github import GitHubError

    github = FakeGitHubClient(error=GitHubError("auth failed", status_code=401))
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._github_for_default_owner", return_value=github),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 503
        assert "authentication failed" in resp.json()["detail"]


def test_space_repos_not_found() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations = [
        {"id": 1001, "account": {"login": "acme-corp", "type": "Organization"}},
    ]
    github = FakeGitHubClient(installations=installations)
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._github_for_default_owner", return_value=github),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces/nonexistent/repos")
        assert resp.status_code == 404
        assert "space not found" in resp.json()["detail"]


def test_repo_detail_requires_auth() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()
    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
    ):
        client = _client(session, redis, settings)
        resp = client.get("/api/v1/admin/repos/acme/repo")
        assert resp.status_code == 401


def test_repo_detail_not_accessible() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    class RejectingRegistry(FakeOwnerRegistry):
        def resolve(self, full_name: str, installation_id: int | None = None, explicit_owner: str | None = None) -> Any:
            return SimpleNamespace(owner_id="", reason="unknown_owner", rejected=lambda: True)

    registry = RejectingRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/repos/acme/repo")
        assert resp.status_code == 404
        assert "not found or not accessible" in resp.json()["detail"]


def test_viewer_can_read_spaces() -> None:
    """Viewers have read-only access to spaces endpoints."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations = [
        {"id": 1001, "account": {"login": "acme-corp", "type": "Organization"}},
    ]
    github = FakeGitHubClient(installations=installations)
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._github_for_default_owner", return_value=github),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200


def test_spaces_skips_empty_logins() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations = [
        {"id": 1001, "account": {"login": "acme-corp", "type": "Organization"}},
        {"id": 1002, "account": {"login": "", "type": "User"}},
        {"id": 1003, "account": None},
    ]
    github = FakeGitHubClient(installations=installations)
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._github_for_default_owner", return_value=github),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["login"] == "acme-corp"


def test_spaces_normalizes_unknown_type() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations = [
        {"id": 1001, "account": {"login": "test-bot", "type": "Bot"}},
    ]
    github = FakeGitHubClient(installations=installations)
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._github_for_default_owner", return_value=github),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["type"] == "User"
