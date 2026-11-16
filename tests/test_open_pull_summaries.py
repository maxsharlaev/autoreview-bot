"""Installation repo pages and the one-call open-PR presence probe."""

from __future__ import annotations

import httpx
import pytest
from app.adapters.github import GitHubAppClient


def _client() -> GitHubAppClient:
    client = GitHubAppClient.__new__(GitHubAppClient)

    async def token(owner: str, repo: str) -> str:
        return "token"

    client.installation_token = token  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_installation_repo_page_does_not_walk_further_pages() -> None:
    client = _client()
    calls: list[dict] = []

    async def access_token(installation_id: int) -> str:
        assert installation_id == 7
        return "token"

    async def request(method: str, url: str, **kwargs):  # noqa: ANN003
        calls.append(kwargs["params"])
        return httpx.Response(
            200,
            json={"total_count": 1000, "repositories": [{"full_name": "acme/one"}]},
            headers={"link": '<https://api.github.com/installation/repositories?page=3>; rel="next"'},
        )

    client._installation_access_token = access_token  # type: ignore[method-assign]
    client._request = request  # type: ignore[method-assign]
    batch, has_next, total = await client.list_installation_repos_page(7, page=2, per_page=50)
    assert calls == [{"per_page": 50, "page": 2}]
    assert [item["full_name"] for item in batch] == ["acme/one"]
    assert has_next is True
    assert total == 1000


@pytest.mark.asyncio
async def test_presence_probe_requests_a_single_pull() -> None:
    client = _client()

    async def request(method: str, url: str, **kwargs):  # noqa: ANN003
        assert kwargs["params"] == {"state": "open", "per_page": 1, "page": 1}
        return httpx.Response(200, json=[])

    client._request = request  # type: ignore[method-assign]
    assert await client.has_open_pull("acme", "repo") is False


@pytest.mark.asyncio
async def test_pull_page_reports_next_link() -> None:
    client = _client()

    async def request(method: str, url: str, **kwargs):  # noqa: ANN003
        assert kwargs["params"] == {"state": "open", "per_page": 50, "page": 2}
        return httpx.Response(
            200,
            json=[{"number": 9}],
            headers={"link": '<https://api.github.com/repos/acme/repo/pulls?page=3>; rel="next"'},
        )

    client._request = request  # type: ignore[method-assign]
    batch, has_next = await client.list_open_pulls_page("acme", "repo", page=2, per_page=50)
    assert [item["number"] for item in batch] == [9]
    assert has_next is True
