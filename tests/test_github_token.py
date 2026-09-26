from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from app.adapters.github import GitHubAppClient, GitHubError
from app.config import Settings


def _settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {
        "github_token": "",
        "github_app_id": 0,
        "github_app_private_key": "",
        "github_installation_id": 0,
    }
    values.update(kwargs)
    return Settings.model_construct(**values)


@pytest.mark.asyncio
async def test_pat_is_used_instead_of_app_jwt() -> None:
    client = GitHubAppClient(settings=_settings(github_token="github_pat_test"))
    with patch.object(client, "_request", new_callable=AsyncMock) as request:
        token = await client.installation_token("acme", "backend")
    assert token == "github_pat_test"
    request.assert_not_called()


def test_github_pat_alias_accepted() -> None:
    settings = Settings.model_validate({"GITHUB_PAT": "github_pat_from_alias"})
    assert settings.github_token == "github_pat_from_alias"


@pytest.mark.asyncio
async def test_missing_github_credentials() -> None:
    client = GitHubAppClient(settings=_settings())
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        await client.installation_token("acme", "backend")
