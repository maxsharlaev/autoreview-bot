from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.github import GitHubAppClient, GitHubError, PullRequestInfo
from app.config import AppConfig, Settings
from app.models import extract_comment_author_logins
from app.owners.registry import RouteResult
from app.services.orchestrator import _remember_comment_author, run_review
from app.services.size_guard import SIZE_SKIP_MARKER


def _github(login: str) -> GitHubAppClient:
    github = GitHubAppClient(settings=Settings.model_construct(github_token="test-token"))
    github.installation_token = AsyncMock(return_value="test-token")
    github.comment_author_login = AsyncMock(return_value=login)
    return github


@pytest.mark.asyncio
async def test_comment_author_is_persisted_before_publication() -> None:
    repository = SimpleNamespace(id=uuid.uuid4(), full_name="org/repo", comment_authors=["old-bot"], owner_id="default")
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: repository)),
        commit=AsyncMock(),
    )
    github = _github("new-bot")

    await _remember_comment_author(session, github, repository, "org", "repo", owner_id="default")

    # M2 writes object format with owner_id; tolerant reader extracts logins
    logins = extract_comment_author_logins(repository.comment_authors)
    assert logins == ["old-bot", "new-bot"]
    # Verify the new entry is in object format with owner_id
    assert len(repository.comment_authors) == 2
    assert repository.comment_authors[0] == "old-bot"  # Old entry preserved as string
    assert repository.comment_authors[1]["login"] == "new-bot"
    assert repository.comment_authors[1]["owner_id"] == "default"
    assert github.previous_comment_authors == ("old-bot", "new-bot")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_comment_author_registry_ignores_case_only_changes() -> None:
    repository = SimpleNamespace(
        id=uuid.uuid4(), full_name="org/repo", comment_authors=["Review-Bot"], owner_id="default"
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: repository)),
        commit=AsyncMock(),
    )
    github = _github("review-bot")

    await _remember_comment_author(session, github, repository, "org", "repo", owner_id="default")

    # No new entry added since same login (case-insensitive)
    logins = extract_comment_author_logins(repository.comment_authors)
    assert logins == ["Review-Bot"]
    assert github.previous_comment_authors == ("Review-Bot",)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_comment_author_lookup_failure_does_not_block_review(caplog) -> None:
    repository = SimpleNamespace(id=uuid.uuid4(), full_name="org/repo", comment_authors=["old-bot"], owner_id="default")
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    github = _github("new-bot")
    github.previous_comment_authors = ("old-bot",)
    github.comment_author_login = AsyncMock(side_effect=GitHubError("identity unavailable"))

    with caplog.at_level("WARNING"):
        await _remember_comment_author(session, github, repository, "org", "repo", owner_id="default")

    assert github.previous_comment_authors == ("old-bot",)
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    assert "Could not identify GitHub comment author" in caplog.text


@pytest.mark.asyncio
async def test_hard_limit_uses_persisted_author_after_credential_change() -> None:
    info = PullRequestInfo(
        number=7,
        title="feat: validate",
        body="",
        html_url="",
        author="author",
        assignee=None,
        state="open",
        draft=False,
        is_fork=False,
        base_sha="b" * 40,
        head_sha="a" * 40,
        head_ref="feature/validation",
        base_ref="main",
        owner="org",
        repo="repo",
        full_name="org/repo",
    )
    repository = SimpleNamespace(id=uuid.uuid4(), full_name="org/repo", comment_authors=["old-bot"], owner_id="default")
    pr = SimpleNamespace(repository=repository, number=7, findings=[], bot_title=None)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        pull_request=pr,
        status="pending",
        head_sha=info.head_sha,
        summary={"size_metrics": {"commits": 151, "additions": 0, "deletions": 0, "changed_files": 1}},
        trigger="webhook",
        owner_id="default",
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one=lambda: run),
                SimpleNamespace(scalar_one=lambda: repository),
            ]
        ),
        commit=AsyncMock(),
    )
    github = _github("new-bot")
    github.get_pull_request = AsyncMock(return_value=info)
    github.collaborator_permission = AsyncMock(return_value="write")
    github.upsert_sticky_comment = AsyncMock()

    # Mock registry that returns the default owner
    mock_ctx = SimpleNamespace(
        config=AppConfig(),
        openai_api_key="test-key",
        jira=None,
        slack=None,
    )
    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_ctx
    mock_registry.resolve.return_value = RouteResult("default", "exact")

    result = await run_review(
        session,
        run.id,
        settings=github.settings,
        config=AppConfig(),
        github=github,
        publisher=SimpleNamespace(
            jira=SimpleNamespace(enabled=lambda: False), slack=SimpleNamespace(enabled=lambda: False)
        ),
        registry=mock_registry,
    )

    assert result.status == "skipped"
    logins = extract_comment_author_logins(repository.comment_authors)
    assert logins == ["old-bot", "new-bot"]
    assert github.previous_comment_authors == ("old-bot", "new-bot")
    assert github.upsert_sticky_comment.await_args.kwargs["marker"] == SIZE_SKIP_MARKER
