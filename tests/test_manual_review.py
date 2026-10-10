from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.api.deps import Principal, get_session, require_principal
from app.api.v1 import api_router
from app.config import AppConfig, GitHubYaml, repo_allowed
from app.services.constants import SKIP_REPO
from app.services.manual_review import ManualReviewRequest, normalize_repo_full_name
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_normalize_owner_repo() -> None:
    assert normalize_repo_full_name("example-org/example-repo") == ("example-org/example-repo")


def test_normalize_github_url() -> None:
    assert normalize_repo_full_name("https://github.com/example-org/example-repo") == "example-org/example-repo"


def test_manual_request_from_pull_url() -> None:
    body = ManualReviewRequest(pull_url="https://github.com/example-org/example-repo/pull/12")
    assert body.repository == "example-org/example-repo"
    assert body.number == 12
    assert body.force is True


def test_manual_request_requires_repo_and_number() -> None:
    with pytest.raises(ValidationError):
        ManualReviewRequest(repository="example-org/example-repo")


def test_allowlist_multiple_repos() -> None:
    config = AppConfig(
        github=GitHubYaml(
            allowed_repos=[
                "example-org/example-repo",
                "example-org/another-repo",
            ]
        )
    )
    assert repo_allowed("example-org/example-repo", config)
    assert repo_allowed("example-org/another-repo", config)
    assert not repo_allowed("someone/else", config)


async def _fake_session() -> AsyncIterator[object]:
    yield MagicMock()


def _client(*, with_api_key: bool) -> TestClient:
    application = FastAPI()
    application.include_router(api_router)
    application.dependency_overrides[get_session] = _fake_session
    application.state.redis = AsyncMock()
    if with_api_key:
        # For /reviews endpoint, override require_principal to return an operator principal
        application.dependency_overrides[require_principal] = lambda: Principal(kind="operator", owner_id=None)
    return TestClient(application)


def test_manual_review_requires_access_key() -> None:
    response = _client(with_api_key=False).post(
        "/api/v1/reviews",
        json={"repository": "example-org/example-repo", "number": 1},
    )
    assert response.status_code == 401


def _mock_settings() -> MagicMock:
    """Create mock settings for registry tests."""
    mock = MagicMock()
    mock.github_token = ""
    mock.github_app_id = 0
    mock.github_app_private_key = ""
    mock.github_installation_id = 0
    mock.github_webhook_secret = ""
    mock.jira_base_url = ""
    mock.jira_email = ""
    mock.jira_api_token = ""
    mock.slack_bot_token = ""
    mock.openai_api_key = ""
    mock.review_api_key = ""
    mock.github_private_key_pem.return_value = ""
    return mock


def test_manual_review_rejects_unknown_repo() -> None:
    """Test that a repo not in the allowlist is rejected with real registry."""
    from app.owners.registry import OwnerRegistry

    # Build a real registry with a specific allowlist
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "default": True,
                "github": {
                    "auth": "pat",
                    "allowed_repos": ["org-a/allowed-repo"],  # Only this repo is allowed
                },
            },
        }
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_test_token"}

    registry = OwnerRegistry.build(settings, config, env=env)

    with patch("app.api.v1.reviews.get_owner_registry", return_value=registry):
        response = _client(with_api_key=True).post(
            "/api/v1/reviews",
            json={"repository": "other/repo", "number": 1},  # Not in allowlist
        )
    assert response.status_code == 403
    assert response.json()["detail"] == SKIP_REPO
