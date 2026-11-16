"""Open-PR summaries: GraphQL totals, with a one-page presence fallback."""

from __future__ import annotations

import httpx
import pytest
from app.adapters.github import GitHubAppClient, GitHubError


def _client() -> GitHubAppClient:
    client = GitHubAppClient.__new__(GitHubAppClient)

    async def token(owner: str, repo: str) -> str:
        return "token"

    client.installation_token = token  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_summary_reads_batched_totals() -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    async def request(method: str, url: str, **kwargs):  # noqa: ANN003
        calls.append((method, url))
        assert method == "POST"
        assert url.endswith("/graphql")
        query = kwargs["json"]["query"]
        assert "acme/one" not in query
        assert 'owner: "acme"' in query
        assert 'name: "one"' in query
        assert 'name: "two"' in query
        return httpx.Response(
            200,
            json={
                "data": {
                    "r0": {"pullRequests": {"totalCount": 3}},
                    "r1": {"pullRequests": {"totalCount": 0}},
                }
            },
        )

    client._request = request  # type: ignore[method-assign]
    summaries = await client.open_pull_summaries(["acme/one", "acme/two", "acme/one"])
    assert calls == [("POST", "https://api.github.com/graphql")]
    assert summaries["acme/one"].count == 3
    assert summaries["acme/one"].has_open is True
    assert summaries["acme/two"].is_empty is True


@pytest.mark.asyncio
async def test_summary_probes_presence_when_graphql_misses_a_repo() -> None:
    client = _client()
    calls: list[str] = []

    async def request(method: str, url: str, **kwargs):  # noqa: ANN003
        calls.append(url)
        if url.endswith("/graphql"):
            return httpx.Response(
                200,
                json={"data": {"r0": None, "r1": {"pullRequests": {"totalCount": 2}}}, "errors": [{"path": ["r0"]}]},
            )
        assert kwargs["params"]["per_page"] == 1
        return httpx.Response(200, json=[{"number": 1}])

    client._request = request  # type: ignore[method-assign]
    summaries = await client.open_pull_summaries(["acme/missing", "acme/known"])
    assert summaries["acme/known"].count == 2
    assert summaries["acme/missing"].count is None
    assert summaries["acme/missing"].has_open is True
    assert any(url.endswith("/repos/acme/missing/pulls") for url in calls)
    assert not any(url.endswith("/repos/acme/known/pulls") for url in calls)


@pytest.mark.asyncio
async def test_summary_probes_presence_when_graphql_fails() -> None:
    client = _client()

    async def request(method: str, url: str, **kwargs):  # noqa: ANN003
        if url.endswith("/graphql"):
            raise GitHubError("down", status_code=502)
        return httpx.Response(200, json=[])

    client._request = request  # type: ignore[method-assign]
    summaries = await client.open_pull_summaries(["acme/repo"])
    assert summaries["acme/repo"].count is None
    assert summaries["acme/repo"].has_open is False
    assert summaries["acme/repo"].is_empty is True


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
