from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.github import PullRequestInfo
from app.config import AppConfig, PrDescriptionYaml, SizeGuardYaml
from app.owners.registry import RouteResult
from app.services.orchestrator import _complete_then_describe, run_review
from app.services.size_guard import SIZE_SKIP_MARKER, classify_pr_size


def _info() -> PullRequestInfo:
    return PullRequestInfo(
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


def test_soft_and_hard_size_limits_and_override() -> None:
    guard = SizeGuardYaml()
    info = _info()
    info.commits_count = 50
    info.additions = 5000
    assert classify_pr_size(info, guard) == "normal"
    info.commits_count = 51
    assert classify_pr_size(info, guard) == "soft"
    info.commits_count = 151
    assert classify_pr_size(info, guard) == "hard"
    info.labels = ("AutoReview:Force",)
    assert classify_pr_size(info, guard) == "normal"


def test_lines_also_trigger_size_limits() -> None:
    info = _info()
    info.additions = 4000
    info.deletions = 1001
    assert classify_pr_size(info, SizeGuardYaml()) == "soft"
    info.deletions = 16001
    assert classify_pr_size(info, SizeGuardYaml()) == "hard"


def test_size_guard_rejects_reversed_thresholds() -> None:
    with pytest.raises(ValueError):
        SizeGuardYaml.model_validate({"soft": {"commits": 151, "changed_lines": 5000}})


@pytest.mark.asyncio
async def test_hard_limit_skips_before_model_or_file_requests() -> None:
    info = _info()
    repo = SimpleNamespace(full_name="org/repo", owner_id="default", comment_authors=[])
    pr = SimpleNamespace(repository=repo, number=7, findings=[], bot_title=None)
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
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: run)),
        commit=AsyncMock(),
    )
    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        collaborator_permission=AsyncMock(return_value="write"),
        upsert_sticky_comment=AsyncMock(),
        list_files=AsyncMock(),
    )

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

    codex = AsyncMock()
    result = await run_review(
        session,
        run.id,
        settings=SimpleNamespace(),
        config=AppConfig(),
        github=github,
        jira=SimpleNamespace(),
        publisher=SimpleNamespace(),
        codex_fn=codex,
        registry=mock_registry,
    )
    assert result.status == "skipped"
    assert github.upsert_sticky_comment.await_args.kwargs["marker"] == SIZE_SKIP_MARKER
    assert github.upsert_sticky_comment.await_args.kwargs["can_replace"]("existing") is False
    github.list_files.assert_not_awaited()
    codex.assert_not_awaited()


@pytest.mark.asyncio
async def test_soft_limit_preserves_review_and_skips_description(monkeypatch) -> None:
    complete = AsyncMock(return_value="completed")
    monkeypatch.setattr("app.services.orchestrator._complete", complete)
    description = AsyncMock()
    result = await _complete_then_describe(
        session=SimpleNamespace(),
        run=SimpleNamespace(),
        started=0,
        verified=SimpleNamespace(),
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True)),
        prompt_version="v1",
        progress=SimpleNamespace(),
        description_step=description,
        skip_description=True,
    )
    assert result == "completed"
    complete.assert_awaited_once()
    description.assert_not_awaited()
