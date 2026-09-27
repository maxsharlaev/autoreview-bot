from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from app.adapters.github import GitHubAppClient, GitHubError
from app.config import Settings
from app.services.pr_description import COMMENT_MARKER, description_comment_is_intact, render_pr_description_comment


def _client() -> GitHubAppClient:
    return GitHubAppClient(settings=Settings.model_construct(github_token="test-token"))


@pytest.mark.asyncio
async def test_description_comment_updates_existing_marker() -> None:
    client = _client()
    request = AsyncMock()
    request.side_effect = [
        _response(
            [{"id": 10, "body": "<!-- open-pr-review -->"}, {"id": 11, "body": render_pr_description_comment("old")}]
        ),
        _response({}),
    ]
    client._request = request
    await client.upsert_sticky_comment(
        "org",
        "repo",
        7,
        render_pr_description_comment("new"),
        marker=COMMENT_MARKER,
        can_replace=description_comment_is_intact,
    )
    assert request.await_args_list[1].args[:2] == ("PATCH", "https://api.github.com/repos/org/repo/issues/comments/11")


@pytest.mark.asyncio
async def test_commit_messages_are_loaded_from_pr() -> None:
    client = _client()
    client._request = AsyncMock(return_value=_response([{"commit": {"message": "feat: validate"}}]))
    assert await client.list_pull_commit_messages("org", "repo", 7) == ["feat: validate"]
    assert "/pulls/7/commits" in client._request.await_args.args[1]


@pytest.mark.asyncio
async def test_default_template_is_read_from_base_branch() -> None:
    client = _client()
    encoded = base64.b64encode(b"## Summary\n\nFill me in").decode()
    client._request = AsyncMock(return_value=_response({"encoding": "base64", "content": encoded}))
    assert await client.get_default_pr_template("org", "repo", "main") == "## Summary\n\nFill me in"
    assert client._request.await_args.kwargs["params"] == {"ref": "main"}


@pytest.mark.asyncio
async def test_missing_template_is_not_treated_as_author_text() -> None:
    client = _client()
    client._request = AsyncMock(side_effect=GitHubError("GET template -> 404: Not Found"))
    assert await client.get_default_pr_template("org", "repo", "main") is None


@pytest.mark.asyncio
async def test_selected_template_must_match_body() -> None:
    client = _client()
    client.get_default_pr_template = AsyncMock(return_value=None)
    encoded = base64.b64encode(b"## Change\n\nFill me in").decode()
    client._request = AsyncMock(
        side_effect=[
            _response([{"type": "file", "name": "feature.md", "path": ".github/PULL_REQUEST_TEMPLATE/feature.md"}]),
            _response({"encoding": "base64", "content": encoded}),
        ]
    )
    template = await client.get_matching_pr_template("org", "repo", "main", "## Change\n\nFill me in")
    assert template == "## Change\n\nFill me in"
    assert client._request.await_args.kwargs["params"] == {"ref": "main"}


@pytest.mark.asyncio
async def test_edited_description_comment_is_preserved() -> None:
    client = _client()
    client._request = AsyncMock(
        return_value=_response([{"id": 11, "body": render_pr_description_comment("old") + "edited"}])
    )
    await client.upsert_sticky_comment(
        "org",
        "repo",
        7,
        render_pr_description_comment("new"),
        marker=COMMENT_MARKER,
        can_replace=description_comment_is_intact,
    )
    assert client._request.await_count == 1


@pytest.mark.asyncio
async def test_title_update_changes_only_title() -> None:
    client = _client()
    client._request = AsyncMock(return_value=_response({}))
    await client.update_pull_request_title("org", "repo", 7, "Validate form inputs")
    assert client._request.await_args.kwargs["json"] == {"title": "Validate form inputs"}


def _response(value):
    class Response:
        def json(self):
            return value

    return Response()
