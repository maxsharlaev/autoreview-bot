from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from app.adapters.github import GitHubAppClient, GitHubError
from app.config import Settings
from app.services.pr_description import COMMENT_MARKER, description_comment_is_intact, render_pr_description_comment
from app.services.size_guard import SIZE_SKIP_MARKER


def _client() -> GitHubAppClient:
    client = GitHubAppClient(settings=Settings.model_construct(github_token="test-token"))
    client.comment_author_login = AsyncMock(return_value="review-bot")
    return client


def _comment(comment_id: int, body: str, author: str = "review-bot") -> dict:
    return {"id": comment_id, "body": body, "user": {"login": author}}


@pytest.mark.asyncio
async def test_description_comment_updates_existing_marker() -> None:
    client = _client()
    request = AsyncMock()
    request.side_effect = [
        _response([_comment(10, "<!-- open-pr-review -->"), _comment(11, render_pr_description_comment("old"))]),
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
async def test_comment_with_matching_marker_from_another_author_is_ignored() -> None:
    client = _client()
    request = AsyncMock(
        side_effect=[
            _response([_comment(10, render_pr_description_comment("old"), author="another-user")]),
            _response({}),
        ]
    )
    client._request = request
    await client.upsert_sticky_comment(
        "org",
        "repo",
        7,
        render_pr_description_comment("new"),
        marker=COMMENT_MARKER,
        can_replace=description_comment_is_intact,
    )
    assert request.await_args_list[1].args[:2] == ("POST", "https://api.github.com/repos/org/repo/issues/7/comments")


@pytest.mark.asyncio
async def test_own_comment_is_selected_after_foreign_marker() -> None:
    client = _client()
    request = AsyncMock(
        side_effect=[
            _response(
                [
                    _comment(10, render_pr_description_comment("old"), author="another-user"),
                    _comment(11, render_pr_description_comment("old"), author="review-bot"),
                ]
            ),
            _response({}),
        ]
    )
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
async def test_comment_author_is_cached_across_clients_for_pat() -> None:
    client = GitHubAppClient(settings=Settings.model_construct(github_token="cache-test-token"))
    client._request = AsyncMock(return_value=_response({"login": "review-bot"}))
    assert await client.comment_author_login("cache-test-token") == "review-bot"
    assert client._request.await_args.args == ("GET", "https://api.github.com/user")
    next_client = GitHubAppClient(settings=Settings.model_construct(github_token="cache-test-token"))
    next_client._request = AsyncMock(side_effect=AssertionError("unexpected identity request"))
    assert await next_client.comment_author_login("cache-test-token") == "review-bot"
    assert client._request.await_count == 1
    next_client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_author_is_resolved_from_github_app() -> None:
    client = GitHubAppClient(settings=Settings.model_construct(github_token="", github_app_id=8971))
    client._jwt = lambda: "app-jwt"
    client._request = AsyncMock(return_value=_response({"slug": "review-app"}))
    assert await client.comment_author_login("installation-token") == "review-app[bot]"
    assert client._request.await_args.args == ("GET", "https://api.github.com/app")
    assert client._request.await_args.kwargs["token"] == "app-jwt"
    next_client = GitHubAppClient(settings=Settings.model_construct(github_token="", github_app_id=8971))
    next_client._request = AsyncMock(side_effect=AssertionError("unexpected identity request"))
    assert await next_client.comment_author_login("another-installation-token") == "review-app[bot]"
    next_client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_comment_author_is_reused_after_credentials_change() -> None:
    client = GitHubAppClient(
        settings=Settings.model_construct(github_token="new-token"),
        previous_comment_authors=("old-bot",),
    )
    client.comment_author_login = AsyncMock(return_value="new-bot")
    client._request = AsyncMock(
        side_effect=[
            _response([_comment(42, render_pr_description_comment("old"), author="old-bot")]),
            _response({}),
        ]
    )
    await client.upsert_sticky_comment(
        "org",
        "repo",
        7,
        render_pr_description_comment("new"),
        marker=COMMENT_MARKER,
        can_replace=description_comment_is_intact,
    )
    assert client._request.await_args.args[:2] == ("PATCH", "https://api.github.com/repos/org/repo/issues/comments/42")


@pytest.mark.asyncio
async def test_identity_lookup_failure_uses_marker_and_logs_warning(caplog) -> None:
    client = GitHubAppClient(settings=Settings.model_construct(github_token="failure-token"))
    client._request = AsyncMock(
        side_effect=[
            GitHubError("identity unavailable"),
            _response([_comment(42, "<!-- open-pr-review -->", author="old-bot")]),
            _response({}),
        ]
    )
    with caplog.at_level("WARNING"):
        await client.upsert_sticky_comment("org", "repo", 7, "<!-- open-pr-review -->\nUpdated")
    assert "using marker lookup" in caplog.text
    assert client._request.await_args.args[:2] == ("PATCH", "https://api.github.com/repos/org/repo/issues/comments/42")


@pytest.mark.asyncio
async def test_commit_messages_are_loaded_from_pr() -> None:
    client = _client()
    client._request = AsyncMock(return_value=_response([{"commit": {"message": "feat: validate"}}]))
    assert await client.list_pull_commit_messages("org", "repo", 7) == ["feat: validate"]
    assert "/pulls/7/commits" in client._request.await_args.args[1]


@pytest.mark.asyncio
async def test_commit_messages_stop_at_github_cap() -> None:
    client = _client()
    full_page = [{"commit": {"message": f"feat: item {i}"}} for i in range(100)]
    client._request = AsyncMock(side_effect=[_response(full_page)] * 4)
    messages = await client.list_pull_commit_messages("org", "repo", 7)
    assert len(messages) == 250
    assert client._request.await_count == 3


@pytest.mark.asyncio
async def test_size_skip_comment_is_not_repeated_on_later_pushes() -> None:
    client = _client()
    client._request = AsyncMock(return_value=_response([_comment(12, SIZE_SKIP_MARKER + "\nPrevious size warning")]))
    await client.upsert_sticky_comment(
        "org",
        "repo",
        7,
        SIZE_SKIP_MARKER + "\nNew warning",
        marker=SIZE_SKIP_MARKER,
        can_replace=lambda _body: False,
    )
    assert client._request.await_count == 1


@pytest.mark.asyncio
async def test_foreign_size_marker_does_not_hide_skip_notice() -> None:
    client = _client()
    client._request = AsyncMock(
        side_effect=[
            _response([_comment(12, SIZE_SKIP_MARKER, author="another-user")]),
            _response({}),
        ]
    )
    await client.upsert_sticky_comment(
        "org",
        "repo",
        7,
        SIZE_SKIP_MARKER + "\nSize warning",
        marker=SIZE_SKIP_MARKER,
        can_replace=lambda _body: False,
    )
    assert client._request.await_args.args[:2] == ("POST", "https://api.github.com/repos/org/repo/issues/7/comments")


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
    client._request = AsyncMock(side_effect=GitHubError("GET template -> 404: Not Found", status_code=404))
    assert await client.get_default_pr_template("org", "repo", "main") is None


@pytest.mark.asyncio
async def test_template_error_with_404_text_but_other_status_is_not_ignored() -> None:
    client = _client()
    client._request = AsyncMock(side_effect=GitHubError("body says 404", status_code=403))
    with pytest.raises(GitHubError):
        await client.get_default_pr_template("org", "repo", "main")


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
    client._request = AsyncMock(return_value=_response([_comment(11, render_pr_description_comment("old") + "edited")]))
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
