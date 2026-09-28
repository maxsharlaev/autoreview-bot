from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.adapters.github import GitHubAppClient
from app.api.deps import require_result_api_key
from app.api.v1.pull_request import pull_request_webhook
from app.config import PublicReposYaml, Settings
from app.models import Finding
from app.services.comment_render import render_jira_comment, render_sticky_comment
from app.services.orchestrator import _carry_open_previous, _save_review_result, _store_findings
from app.services.public_cleanup import redact_public_repository
from app.services.publisher import empty_verified
from fastapi import HTTPException
from tests.test_public_disclosure import SECRET, _render, _security


def _finding(*, visibility: str | None) -> Finding:
    return Finding(
        id=uuid.uuid4(),
        stable_id="secret-finding",
        severity="P2",
        category="correctness",
        path="src/auth.py",
        line=42,
        title=SECRET,
        scenario=SECRET,
        evidence=SECRET,
        recommendation=SECRET,
        details_visibility=visibility,
        current_status="open",
    )


def test_public_repeat_preserves_public_context_and_hides_private_context() -> None:
    for provenance, expected in [("public", SECRET), ("private", ""), (None, "")]:
        fresh = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
        fresh.findings = [_security()]
        fresh.findings[0].scenario = ""
        _carry_open_previous(fresh, [_finding(visibility=provenance)], visibility="public")
        assert fresh.findings[0].scenario == expected


def test_public_repeat_can_render_fresh_details_under_explicit_full_policy() -> None:
    render = _render(PublicReposYaml(security_findings="full"))
    render.verified.findings[0].category = "correctness"
    render.verified.summary = "Safe summary"
    assert SECRET in render_sticky_comment(render)
    render.redacted_prior_ids = {"secret-finding"}
    assert SECRET not in render_sticky_comment(render)


def test_default_public_redacts_misclassified_security_details() -> None:
    render = _render()
    render.verified.findings[0].category = "correctness"
    assert SECRET not in render_sticky_comment(render)
    assert SECRET in render_jira_comment(render)


@pytest.mark.asyncio
async def test_incomplete_public_repeat_keeps_private_database_details() -> None:
    prior = _finding(visibility="private")
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    view = _security()
    view.scenario = ""
    verified.findings = [view]
    session = SimpleNamespace(add=lambda _: None, flush=AsyncMock())
    pr = SimpleNamespace(findings=[prior])
    run = SimpleNamespace(id=uuid.uuid4())
    await _store_findings(session, pr, run, verified, fresh_ids={view.stable_id}, details_visibility="public")
    assert prior.scenario == SECRET
    assert prior.details_visibility == "private"


@pytest.mark.asyncio
async def test_full_result_is_committed_before_publication() -> None:
    added = []
    session = SimpleNamespace(add=added.append, commit=AsyncMock())
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.findings = [_security()]
    await _save_review_result(
        session, SimpleNamespace(id=uuid.uuid4(), head_sha="a" * 40), verified, [], "private", "public"
    )
    session.commit.assert_awaited_once()
    assert added[0].payload["verified"]["findings"][0]["scenario"] == SECRET
    assert added[0].payload["publication_visibility"] == "public"


@pytest.mark.asyncio
async def test_result_access_requires_distinct_operator_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.deps.get_settings",
        lambda: Settings.model_construct(review_api_key="shared", result_api_key="shared"),
    )
    with pytest.raises(HTTPException) as disabled:
        await require_result_api_key(authorization="Bearer shared", x_api_key=None)
    assert disabled.value.status_code == 503
    monkeypatch.setattr(
        "app.api.deps.get_settings",
        lambda: Settings.model_construct(review_api_key="shared", result_api_key="private"),
    )
    with pytest.raises(HTTPException) as denied:
        await require_result_api_key(authorization="Bearer shared", x_api_key=None)
    assert denied.value.status_code == 401
    await require_result_api_key(authorization="Bearer private", x_api_key=None)


@pytest.mark.asyncio
async def test_public_cleanup_redacts_bot_content_and_commits() -> None:
    repository = SimpleNamespace(
        id=uuid.uuid4(), full_name="org/repo", comment_authors=["old-bot"], last_visibility="private"
    )
    pr = SimpleNamespace(number=7, bot_title=SECRET, title=SECRET)
    results = [
        SimpleNamespace(scalar_one_or_none=lambda: repository),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [pr])),
    ]
    session = SimpleNamespace(execute=AsyncMock(side_effect=results), commit=AsyncMock())
    github = SimpleNamespace(
        previous_comment_authors=(),
        get_pull_request=AsyncMock(return_value=SimpleNamespace(visibility="public", body=SECRET, title=SECRET)),
        redact_managed_comments=AsyncMock(return_value=2),
        update_pull_request_body=AsyncMock(),
        update_pull_request_title=AsyncMock(),
    )
    count = await redact_public_repository(session, github, "org/repo")
    assert count == 3
    assert github.previous_comment_authors == ("old-bot",)
    assert repository.last_visibility == "public"
    github.update_pull_request_title.assert_awaited_once()
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_public_webhook_queues_repository_cleanup(monkeypatch) -> None:
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    request = SimpleNamespace(
        body=AsyncMock(return_value=b"signed"),
        json=AsyncMock(return_value={"repository": {"full_name": "org/repo"}}),
    )
    monkeypatch.setattr("app.api.v1.pull_request.is_webhook_enabled", lambda: True)
    monkeypatch.setattr("app.api.v1.pull_request.verify_github_signature", lambda **_kwargs: True)
    monkeypatch.setattr("app.api.v1.pull_request.get_redis", lambda _request: redis)
    response = await pull_request_webhook(request, SimpleNamespace(), x_github_event="public")
    assert response["status"] == "queued"
    redis.enqueue_job.assert_awaited_once_with("publicize_repository", "org/repo")


@pytest.mark.asyncio
async def test_cleanup_only_edits_trusted_bot_comments() -> None:
    github = GitHubAppClient(settings=Settings.model_construct(github_token="test"))
    github.previous_comment_authors = ("old-bot",)
    github.installation_token = AsyncMock(return_value="token")
    github.comment_author_login = AsyncMock(return_value="new-bot")
    comments = [
        {"id": 1, "user": {"login": "old-bot"}, "body": "<!-- open-pr-review --> private"},
        {"id": 2, "user": {"login": "new-bot"}, "body": "<!-- open-pr-review --> private"},
        {"id": 3, "user": {"login": "stranger"}, "body": "<!-- open-pr-review --> private"},
    ]
    github._request = AsyncMock(return_value=SimpleNamespace(json=lambda: comments))
    count = await github.redact_managed_comments(
        "org", "repo", 7, {"<!-- open-pr-review -->": "<!-- open-pr-review --> redacted"}
    )
    assert count == 2
    patches = [call for call in github._request.await_args_list if call.args[0] == "PATCH"]
    assert len(patches) == 2
    assert all("/issues/comments/3" not in call.args[1] for call in patches)
