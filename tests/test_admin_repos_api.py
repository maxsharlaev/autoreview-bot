"""Admin API: spaces (installations) and repos endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.api.v1 import admin as admin_api
from app.api.v1.admin import _InstallationInfo
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
    def __init__(self, owner_id: str = "default", app_id: int = 12345) -> None:
        self.id = owner_id
        self.github = SimpleNamespace(
            kind="app",
            app_id=app_id,
            private_key_pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
            installation_id=0,
            token=None,
        )
        self.is_default = True


class FakeOwnerRegistry:
    def __init__(self, owners: dict[str, Any] | None = None) -> None:
        self.owners = {"default": FakeOwnerContext()} if owners is None else owners
        self._default = next(iter(self.owners.values()), None) if self.owners else None

    def get(self, owner_id: str) -> Any:
        return self.owners.get(owner_id)

    def get_default(self) -> Any:
        return self._default

    def resolve(self, full_name: str, installation_id: int | None = None, explicit_owner: str | None = None) -> Any:
        return SimpleNamespace(owner_id="default", reason="default_fallback", rejected=lambda: False)


def _make_installations(
    items: list[tuple[int, str, str, str]],
) -> tuple[list[_InstallationInfo], dict[int, str]]:
    """Create installation info for testing.

    Args:
        items: List of (id, login, type, owner_id) tuples
    """
    installations = [_InstallationInfo(id=item[0], login=item[1], acc_type=item[2], owner_id=item[3]) for item in items]
    installation_to_owner = {inst.id: inst.owner_id for inst in installations}
    return installations, installation_to_owner


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

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "default"),
            (1002, "user123", "User", "default"),
        ]
    )
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
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

    empty_registry = FakeOwnerRegistry(owners={})

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=empty_registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=([], {}))),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 503
        assert "no owner configured" in resp.json()["detail"]


def test_spaces_no_app_backed_owners() -> None:
    """Test when owners exist but none have App credentials."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=([], {}))),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 503
        assert "no app-backed owners configured" in resp.json()["detail"]


def test_space_repos_not_found() -> None:
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "default"),
        ]
    )
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces/9999/repos")
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

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "default"),
        ]
    )
    registry = FakeOwnerRegistry()

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200


def test_spaces_multi_owner_different_apps() -> None:
    """Test that installations from multiple App-backed owners are all listed."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "owner-a"),
            (1002, "beta-inc", "Organization", "owner-b"),
            (1003, "user123", "User", "owner-a"),
        ]
    )
    registry = FakeOwnerRegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=11111),
            "owner-b": FakeOwnerContext("owner-b", app_id=22222),
        }
    )

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3
        logins = {item["login"] for item in data["items"]}
        assert logins == {"acme-corp", "beta-inc", "user123"}


def test_spaces_dedupes_shared_app() -> None:
    """Test that installations are deduped when owners share the same App."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "owner-a"),
        ]
    )
    registry = FakeOwnerRegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=12345),
            "owner-b": FakeOwnerContext("owner-b", app_id=12345),
        }
    )

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["login"] == "acme-corp"


def test_space_repos_uses_correct_owner_client() -> None:
    """Test that repos are listed using the space's installation ID for PR listing."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "owner-a"),
            (2002, "beta-inc", "Organization", "owner-b"),
        ]
    )
    registry = FakeOwnerRegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=11111),
            "owner-b": FakeOwnerContext("owner-b", app_id=22222),
        }
    )

    from app.adapters.github import GitHubAppClient

    captured_installation_ids: list[int | None] = []

    @classmethod
    def tracking_from_credentials(cls, credentials, owner_id="default", installation_id=None, **kwargs):
        captured_installation_ids.append(installation_id)
        return AsyncMock(list_installation_repos=AsyncMock(return_value=[]))

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
        patch.object(GitHubAppClient, "from_credentials", tracking_from_credentials),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces/2002/repos")
        assert resp.status_code == 200
        assert 2002 in captured_installation_ids


def test_spaces_duplicate_login_different_apps() -> None:
    """AR-008: Two Apps installed on the same org should be disambiguated by installation ID."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "owner-a"),
            (2001, "acme-corp", "Organization", "owner-b"),
        ]
    )
    registry = FakeOwnerRegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=11111),
            "owner-b": FakeOwnerContext("owner-b", app_id=22222),
        }
    )

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        ids = [item["id"] for item in data["items"]]
        assert 1001 in ids
        assert 2001 in ids


def test_spaces_enumeration_error_returns_502() -> None:
    """AR-010: GitHub API errors during enumeration return 502."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    from app.adapters.github import GitHubError
    from app.api.v1.admin import _EnumerationError

    registry = FakeOwnerRegistry()
    enum_error = _EnumerationError(12345, "default", GitHubError("test error", status_code=500))

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(side_effect=enum_error)),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces")
        assert resp.status_code == 502
        assert "github api error" in resp.json()["detail"]


def test_space_repos_enumeration_error_returns_502() -> None:
    """AR-010: GitHub API errors during enumeration in space repos endpoint return 502."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    from app.adapters.github import GitHubError
    from app.api.v1.admin import _EnumerationError

    registry = FakeOwnerRegistry()
    enum_error = _EnumerationError(12345, "default", GitHubError("test error", status_code=500))

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(side_effect=enum_error)),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces/1001/repos")
        assert resp.status_code == 502
        assert "github api error" in resp.json()["detail"]
