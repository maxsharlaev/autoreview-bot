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

    class NonAppOwnerContext:
        def __init__(self, owner_id: str = "default") -> None:
            self.id = owner_id
            self.github = SimpleNamespace(
                kind="pat",
                app_id=None,
                private_key_pem=None,
                installation_id=0,
                token="ghp_test",
            )
            self.is_default = True

    registry = FakeOwnerRegistry(owners={"default": NonAppOwnerContext()})

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
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


def test_repo_detail_uses_installation_id_override() -> None:
    """AR-012: Repo detail with installation_id should pass it to from_credentials()."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (2002, "acme-corp", "Organization", "default"),
        ]
    )

    registry = FakeOwnerRegistry()

    from app.adapters.github import GitHubAppClient, GitHubError

    captured_installation_ids: list[int | None] = []

    async def raise_502(*args, **kwargs):
        raise GitHubError("test - captured installation_id", status_code=502)

    @classmethod
    def tracking_from_credentials(cls, credentials, owner_id="default", installation_id=None, **kwargs):
        captured_installation_ids.append(installation_id)
        mock_client = AsyncMock()
        mock_client.list_open_pulls = raise_502
        return mock_client

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
        patch.object(GitHubAppClient, "from_credentials", tracking_from_credentials),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/repos/acme/repo?installation_id=2002")
        assert resp.status_code == 502
        assert 2002 in captured_installation_ids


def test_space_repos_uses_space_client_for_pr_listing() -> None:
    """AR-009: PR listing should use the space's client, not the routed owner's."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (2002, "beta-inc", "Organization", "owner-b"),
        ]
    )

    class RoutingToOwnerARegistry(FakeOwnerRegistry):
        def resolve(self, full_name: str, installation_id: int | None = None, explicit_owner: str | None = None) -> Any:
            return SimpleNamespace(owner_id="owner-a", reason="explicit", rejected=lambda: False)

    registry = RoutingToOwnerARegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=11111),
            "owner-b": FakeOwnerContext("owner-b", app_id=22222),
        }
    )

    from app.adapters.github import GitHubAppClient
    from app.api.v1 import admin as admin_module
    from app.api.v1.admin_schemas import AdminOpenFindings

    created_clients: list[tuple[str, int | None]] = []

    @classmethod
    def tracking_from_credentials(cls, credentials, owner_id="default", installation_id=None, **kwargs):
        created_clients.append((owner_id, installation_id))
        mock_client = AsyncMock()
        mock_client.list_installation_repos = AsyncMock(return_value=[{"full_name": "beta-inc/repo1", "name": "repo1"}])
        mock_client.list_open_pulls = AsyncMock(return_value=[{"number": 1}])
        return mock_client

    async def mock_get_open_findings_by_severity(
        sess: Any, full_name: str, owner_id: str | None = None
    ) -> AdminOpenFindings:
        return AdminOpenFindings(P0=0, P1=0, P2=0, P3=0)

    async def mock_get_last_run_at(sess: Any, full_name: str, owner_id: str | None = None) -> Any:
        return None

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
        patch.object(GitHubAppClient, "from_credentials", tracking_from_credentials),
        patch.object(admin_module, "_get_open_findings_by_severity", mock_get_open_findings_by_severity),
        patch.object(admin_module, "_get_last_run_at", mock_get_last_run_at),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces/2002/repos?hide_empty_prs=false")
        assert resp.status_code == 200

        assert len(created_clients) == 1
        owner_id, inst_id = created_clients[0]
        assert owner_id == "owner-b"
        assert inst_id == 2002


def test_repo_detail_cross_app_uses_installation_owner() -> None:
    """AR-013: Repo detail with installation_id from different App should use that App's client."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (1001, "acme-corp", "Organization", "owner-a"),
            (2002, "acme-corp", "Organization", "owner-b"),
        ]
    )

    class RoutingToOwnerARegistry(FakeOwnerRegistry):
        def resolve(self, full_name: str, installation_id: int | None = None, explicit_owner: str | None = None) -> Any:
            return SimpleNamespace(owner_id="owner-a", reason="explicit", rejected=lambda: False)

    registry = RoutingToOwnerARegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=11111),
            "owner-b": FakeOwnerContext("owner-b", app_id=22222),
        }
    )

    from app.adapters.github import GitHubAppClient, GitHubError

    captured_calls: list[tuple[str, int | None]] = []

    async def raise_502(*args, **kwargs):
        raise GitHubError("test - captured", status_code=502)

    @classmethod
    def tracking_from_credentials(cls, credentials, owner_id="default", installation_id=None, **kwargs):
        captured_calls.append((owner_id, installation_id))
        mock_client = AsyncMock()
        mock_client.list_open_pulls = raise_502
        return mock_client

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
        patch.object(GitHubAppClient, "from_credentials", tracking_from_credentials),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/repos/acme-corp/repo?installation_id=2002")
        assert resp.status_code == 502

        assert len(captured_calls) == 1
        owner_id, inst_id = captured_calls[0]
        assert owner_id == "owner-b"
        assert inst_id == 2002


def test_spaces_empty_installations_returns_empty_list() -> None:
    """AR-015: Valid Apps with no installations should return empty list, not 503."""
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
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []


def test_space_repos_scopes_findings_to_owner() -> None:
    """AR-014: Findings should be scoped to the resolved owner, not just full_name."""
    session = FakeSession()
    redis = FakeRedis()
    settings = _settings()

    installations, inst_map = _make_installations(
        [
            (2002, "beta-inc", "Organization", "owner-b"),
        ]
    )

    class OwnerBRegistry(FakeOwnerRegistry):
        def resolve(self, full_name: str, installation_id: int | None = None, explicit_owner: str | None = None) -> Any:
            return SimpleNamespace(owner_id="owner-b", reason="explicit", rejected=lambda: False)

    registry = OwnerBRegistry(
        owners={
            "owner-b": FakeOwnerContext("owner-b", app_id=22222),
        }
    )

    from app.adapters.github import GitHubAppClient
    from app.api.v1 import admin as admin_module
    from app.api.v1.admin_schemas import AdminOpenFindings

    passed_owner_ids: list[str | None] = []

    @classmethod
    def mock_from_credentials(cls, credentials, owner_id="default", installation_id=None, **kwargs):
        mock_client = AsyncMock()
        mock_client.list_installation_repos = AsyncMock(return_value=[{"full_name": "beta-inc/repo1", "name": "repo1"}])
        mock_client.list_open_pulls = AsyncMock(return_value=[{"number": 1}])
        return mock_client

    async def tracking_get_open_findings(sess: Any, full_name: str, owner_id: str | None = None) -> AdminOpenFindings:
        passed_owner_ids.append(owner_id)
        return AdminOpenFindings(P0=0, P1=0, P2=0, P3=0)

    async def tracking_get_last_run_at(sess: Any, full_name: str, owner_id: str | None = None) -> Any:
        passed_owner_ids.append(owner_id)
        return None

    with (
        patch("app.api.admin_auth.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_settings", return_value=settings),
        patch("app.api.v1.admin.get_owner_registry", return_value=registry),
        patch("app.api.v1.admin._enumerate_all_installations", AsyncMock(return_value=(installations, inst_map))),
        patch.object(GitHubAppClient, "from_credentials", mock_from_credentials),
        patch.object(admin_module, "_get_open_findings_by_severity", tracking_get_open_findings),
        patch.object(admin_module, "_get_last_run_at", tracking_get_last_run_at),
    ):
        client = _client(session, redis, settings)
        _login_as_root(client)
        resp = client.get("/api/v1/admin/spaces/2002/repos?hide_empty_prs=false")
        assert resp.status_code == 200

        assert "owner-b" in passed_owner_ids


def test_owner_id_filter_set_includes_legacy_default() -> None:
    """AR-014: _owner_id_filter_set includes legacy 'default' when owner is legacy_default_alias."""
    from app.api.v1.admin import _owner_id_filter_set

    class LegacyDefaultRegistry(FakeOwnerRegistry):
        def legacy_default_alias(self) -> str | None:
            return "owner-a"

    registry = LegacyDefaultRegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=11111),
        }
    )

    with patch("app.api.v1.admin.get_owner_registry", return_value=registry):
        owner_ids = _owner_id_filter_set("owner-a")
        assert owner_ids == {"owner-a", "default"}

        owner_ids_other = _owner_id_filter_set("owner-b")
        assert owner_ids_other == {"owner-b"}


def test_owner_id_filter_set_no_legacy_alias() -> None:
    """AR-014: _owner_id_filter_set returns only owner_id when no legacy_default_alias."""
    from app.api.v1.admin import _owner_id_filter_set

    class NoLegacyAliasRegistry(FakeOwnerRegistry):
        def legacy_default_alias(self) -> str | None:
            return None

    registry = NoLegacyAliasRegistry(
        owners={
            "owner-a": FakeOwnerContext("owner-a", app_id=11111),
        }
    )

    with patch("app.api.v1.admin.get_owner_registry", return_value=registry):
        owner_ids = _owner_id_filter_set("owner-a")
        assert owner_ids == {"owner-a"}
