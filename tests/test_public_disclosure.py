from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.adapters.github import GitHubAppClient, PullRequestInfo
from app.adapters.jira import JiraIssue
from app.api.v1.pull_request import pull_request_webhook
from app.config import AppConfig, PrDescriptionYaml, PublicReposYaml, Settings
from app.services.comment_render import FindingTransitionView, RenderInput, render_jira_comment, render_sticky_comment
from app.services.constants import SKIP_TOO_LARGE
from app.services.orchestrator import (
    _check_public_alignment,
    _private_publication_status,
    _publish,
    _publish_pr_description,
    _safe_public_fallback,
    run_review,
)
from app.services.pr_description import plan_pr_body_update
from app.services.publisher import Publisher, empty_verified
from app.services.verifier import FindingView, PreviousFindingView
from app.services.visibility import confirmed_visibility, repository_visibility

SECRET = "internal acceptance criteria: transfer funds without approval"


def _info(visibility: str | None = "public") -> PullRequestInfo:
    return PullRequestInfo(
        number=7,
        title="Dev",
        body="",
        html_url="",
        author="author",
        assignee=None,
        state="open",
        draft=False,
        is_fork=False,
        base_sha="b" * 40,
        head_sha="a" * 40,
        head_ref="ABC-1-feature",
        base_ref="main",
        owner="org",
        repo="repo",
        full_name="org/repo",
        visibility=visibility,
    )


def _issue() -> JiraIssue:
    return JiraIssue(
        key="ABC-1",
        summary=SECRET,
        description=SECRET,
        status="In Progress",
        issue_type="Task",
        priority="High",
        acceptance_criteria=SECRET,
    )


def _security(severity: str = "P3") -> FindingView:
    return FindingView(
        stable_id="secret-finding",
        severity=severity,
        confidence=0.9,
        category="Security",
        path="src/auth.py",
        line=42,
        title="Secret token bypass",
        scenario=SECRET,
        evidence="payload=secret-token",
        recommendation="Validate the token server side",
        blocking_candidate=False,
    )


def _render(policy: PublicReposYaml | None = None, visibility: str | None = "public") -> RenderInput:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.summary = SECRET
    verified.task_alignment_status = "unmet"
    verified.issue_key = "ABC-1"
    verified.unmet_acceptance_criteria = [SECRET]
    verified.findings = [_security()]
    return RenderInput(
        repository="org/repo",
        pr_number=7,
        head_sha="a" * 40,
        mode="advisory",
        run_id="run-1",
        issue_key="ABC-1",
        jira_warning=None,
        verified=verified,
        transitions=[],
        duration_ms=1,
        model="test",
        visibility=visibility,
        public_repos=policy,
        public_alignment="partial",
    )


def test_visibility_is_confirmed_and_unknown_fails_closed() -> None:
    assert repository_visibility({"private": True}) == "private"
    assert repository_visibility({"visibility": "public"}) == "public"
    assert repository_visibility({"private": True, "visibility": "public"}) == "public"
    assert repository_visibility({}) is None
    assert confirmed_visibility("private", None) == "public"
    assert confirmed_visibility("private", "public") == "public"
    assert confirmed_visibility("public", "private") == "public"
    assert confirmed_visibility(None, "private") == "private"


def test_public_policy_defaults_and_rejects_unknown_modes() -> None:
    assert AppConfig().public_repos.jira_disclosure == "key_only"
    assert AppConfig().public_repos.security_findings == "redact"
    with pytest.raises(ValueError):
        PublicReposYaml(jira_disclosure="summary")


@pytest.mark.asyncio
async def test_webhook_passes_repository_visibility_to_queue(monkeypatch) -> None:
    payload = {
        "action": "opened",
        "repository": {"full_name": "org/repo", "private": True},
        "pull_request": {
            "number": 7,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
        },
    }
    request = SimpleNamespace(body=AsyncMock(return_value=b"payload"), json=AsyncMock(return_value=payload))
    queue = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), status="pending"))
    monkeypatch.setattr("app.api.v1.pull_request.is_webhook_enabled", lambda: True)
    monkeypatch.setattr("app.api.v1.pull_request.verify_github_signature", lambda **_kwargs: True)
    monkeypatch.setattr("app.api.v1.pull_request.classify_pull_request_event", lambda *_args: None)
    monkeypatch.setattr("app.api.v1.pull_request.get_redis", lambda _request: None)
    monkeypatch.setattr("app.api.v1.pull_request.queue_review", queue)
    await pull_request_webhook(request, SimpleNamespace())
    assert queue.await_args.kwargs["repository_visibility"] == "private"


@pytest.mark.asyncio
async def test_github_pull_response_confirms_visibility() -> None:
    client = GitHubAppClient(settings=Settings.model_construct(github_token="test"))
    client._request = AsyncMock(
        return_value=SimpleNamespace(
            json=lambda: {
                "number": 7,
                "head": {"repo": {"full_name": "org/repo"}},
                "base": {"repo": {"full_name": "org/repo", "private": True}},
            }
        )
    )
    assert (await client.get_pull_request("org", "repo", 7)).visibility == "private"


@pytest.mark.asyncio
async def test_private_visibility_is_rechecked_before_publication() -> None:
    info = _info("private")
    github = SimpleNamespace(get_pull_request=AsyncMock(return_value=_info("public")))
    assert await _private_publication_status(github, info) == "public_fallback"
    github.get_pull_request.return_value = _info("private")
    assert await _private_publication_status(github, info) == "private"
    changed = _info("private")
    changed.head_sha = "c" * 40
    github.get_pull_request.return_value = changed
    assert await _private_publication_status(github, info) == "head_changed"
    github.get_pull_request.side_effect = RuntimeError("GitHub unavailable")
    assert await _private_publication_status(github, info) == "public_fallback"


def test_public_fallback_keeps_private_result_in_memory() -> None:
    original = _render(visibility="private").verified
    safe = _safe_public_fallback(original)
    assert original.summary == SECRET
    assert original.findings[0].scenario == SECRET
    assert SECRET not in safe.summary
    assert safe.findings[0].scenario == ""
    assert safe.findings[0].severity == original.findings[0].severity


@pytest.mark.asyncio
async def test_public_fallback_forces_redaction_even_with_full_policy() -> None:
    original = _render(visibility="private").verified
    publisher = SimpleNamespace(publish=AsyncMock(return_value={}))
    pr = SimpleNamespace(repository=SimpleNamespace(full_name="org/repo"), number=7, head_sha="a" * 40)
    run = SimpleNamespace(id=uuid.uuid4())
    await _publish(
        publisher,
        "org",
        "repo",
        pr,
        run,
        _safe_public_fallback(original),
        [],
        "ABC-1",
        None,
        None,
        0.0,
        AppConfig(public_repos=PublicReposYaml(security_findings="full")),
        visibility="public",
        force_public_redaction=True,
    )
    render = publisher.publish.await_args.kwargs["render"]
    assert render.public_repos.security_findings == "redact"
    assert SECRET not in render_sticky_comment(render)


def test_public_nonsecurity_review_keeps_model_summary() -> None:
    render = _render()
    render.verified.summary = "Useful summary of the changes"
    render.verified.findings[0].category = "correctness"
    text = render_sticky_comment(render)
    assert "Useful summary of the changes" in text
    assert "Review completed. See the findings below." not in text


def test_none_removes_only_linked_jira_key() -> None:
    render = _render(PublicReposYaml(jira_disclosure="none"))
    render.verified.summary = "Use SHA-256 and UTF-8 for CVE-2024, not ABC-1"
    render.verified.findings[0].category = "correctness"
    text = render_sticky_comment(render)
    assert "ABC-1" not in text
    assert "SHA-256" in text
    assert "UTF-8" in text
    assert "CVE-2024" in text


def test_public_previous_finding_hides_old_private_text() -> None:
    render = _render()
    render.verified.summary = "Review of current changes"
    render.verified.findings[0].category = "correctness"
    render.verified.previous_findings = [
        PreviousFindingView(
            stable_id="secret-finding", status="still_open", evidence=SECRET, path="src/auth.py", line=42
        )
    ]
    render.transitions = [
        FindingTransitionView(
            stable_id="secret-finding",
            status="still_open",
            path="src/auth.py",
            line=42,
            severity="P3",
            title=SECRET,
            category="correctness",
        )
    ]
    text = render_sticky_comment(render)
    assert SECRET not in text
    assert "Previously reported issue." in text
    assert "src/auth.py:42" in text


@pytest.mark.parametrize("disclosure", ["key_only", "none"])
def test_public_review_never_publishes_jira_or_security_details(disclosure: str) -> None:
    render = _render(PublicReposYaml(jira_disclosure=disclosure))
    text = render_sticky_comment(render)
    assert SECRET not in text
    assert "payload=secret-token" not in text
    assert "Secret token bypass" not in text
    assert "src/auth.py:42" in text
    assert "[P3]" in text
    assert "`partial`" in text
    assert "Details are hidden because the repository is public." in text
    if disclosure == "key_only":
        assert "ABC-1" in text
        assert "http" not in text
    else:
        assert "ABC-1" not in text


def test_private_review_keeps_full_output() -> None:
    text = render_sticky_comment(_render(visibility="private"))
    assert SECRET in text
    assert "payload=secret-token" in text
    assert "Secret token bypass" in text
    assert "`unmet`" in text


def test_public_transition_hides_old_security_title() -> None:
    render = _render()
    render.transitions = [
        FindingTransitionView(
            stable_id="old-security",
            status="resolved",
            path="src/auth.py",
            line=42,
            severity="P1",
            title=SECRET,
            category="security",
        )
    ]
    text = render_sticky_comment(render)
    assert SECRET not in text
    assert "Potential security issue." in text


def test_explicit_full_security_policy_keeps_finding() -> None:
    text = render_sticky_comment(_render(PublicReposYaml(security_findings="full")))
    assert "Secret token bypass" in text
    assert "payload=secret-token" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["jira", "slack"])
async def test_public_security_details_go_to_configured_private_channel(channel: str) -> None:
    render = _render()
    github = SimpleNamespace(upsert_sticky_comment=AsyncMock())
    jira = SimpleNamespace(
        project_allowed=lambda _key: True,
        enabled=lambda: channel == "jira",
        add_comment=AsyncMock(),
    )
    slack = SimpleNamespace(enabled=lambda: channel == "slack", post_message=AsyncMock())
    config = AppConfig()
    publisher = Publisher(github=github, jira=jira, slack=slack, config=config)
    await publisher.publish(
        owner="org",
        repo="repo",
        pr_number=7,
        render=render,
        has_blockers=False,
        issue_key="ABC-1",
        current_jira_status=None,
    )
    public_text = github.upsert_sticky_comment.await_args.args[-1]
    assert SECRET not in public_text
    assert "Details are hidden" not in public_text
    private_text = jira.add_comment.await_args.args[-1] if channel == "jira" else slack.post_message.await_args.args[-1]
    assert SECRET in private_text
    assert "payload=secret-token" in private_text
    assert "Secret token bypass" in private_text


@pytest.mark.asyncio
async def test_public_alignment_returns_only_fixed_status(tmp_path) -> None:
    codex = AsyncMock(return_value=json.dumps({"status": "partial"}))
    status = await _check_public_alignment(
        codex_fn=codex,
        checkout=tmp_path,
        info=_info(),
        files=[],
        jira_text=SECRET,
        config=AppConfig(),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    assert status == "partial"
    assert SECRET in codex.await_args.kwargs["prompt"]
    assert codex.await_args.kwargs["sandbox"] == "read-only"


@pytest.mark.asyncio
async def test_public_alignment_failure_falls_back_to_unknown(tmp_path) -> None:
    status = await _check_public_alignment(
        codex_fn=AsyncMock(return_value=json.dumps({"status": SECRET})),
        checkout=tmp_path,
        info=_info(),
        files=[],
        jira_text=SECRET,
        config=AppConfig(),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    assert status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("disclosure", ["key_only", "none", "full"])
async def test_public_pr_text_model_never_receives_jira_content(tmp_path, disclosure: str) -> None:
    info = _info()
    info.body = plan_pr_body_update("Human notes", SECRET, mode="append") or ""
    payload = {
        "summary": "Change validation",
        "changes": "Update validation logic",
        "linked_task": SECRET,
        "testing": "Not verified",
        "notes_risks": "None",
        "suggested_title": "feat: validate inputs",
        "title_relevance": "irrelevant",
        "title_reason": "Current title is generic",
        "output_language": "en",
    }
    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        list_pull_commit_messages=AsyncMock(return_value=["feat: validate inputs"]),
        upsert_sticky_comment=AsyncMock(),
    )
    codex = AsyncMock(return_value=json.dumps(payload))
    await _publish_pr_description(
        github=github,
        codex_fn=codex,
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=_issue(),
        config=AppConfig(
            pr_description=PrDescriptionYaml(enabled=True, mode="comment"),
            public_repos=PublicReposYaml(jira_disclosure=disclosure),
        ),
        settings=Settings.model_construct(openai_api_key="test"),
        visibility="public",
    )
    assert SECRET not in codex.await_args.kwargs["prompt"]
    text = github.upsert_sticky_comment.await_args.args[-1]
    if disclosure == "key_only":
        assert "ABC-1" in text
        assert SECRET not in text
    elif disclosure == "none":
        assert "ABC-1" not in text
        assert SECRET not in text
    else:
        assert SECRET in text


@pytest.mark.asyncio
async def test_private_pr_text_retains_jira_context_and_output(tmp_path) -> None:
    info = _info("private")
    payload = {
        "summary": "Change validation",
        "changes": "Update validation logic",
        "linked_task": SECRET,
        "testing": "Not verified",
        "notes_risks": "None",
        "suggested_title": "feat: validate inputs",
        "title_relevance": "irrelevant",
        "title_reason": "Current title is generic",
        "output_language": "en",
    }
    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        list_pull_commit_messages=AsyncMock(return_value=[]),
        upsert_sticky_comment=AsyncMock(),
    )
    codex = AsyncMock(return_value=json.dumps(payload))
    await _publish_pr_description(
        github=github,
        codex_fn=codex,
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=_issue(),
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, mode="comment")),
        settings=Settings.model_construct(openai_api_key="test"),
        visibility="private",
    )
    assert SECRET in codex.await_args.kwargs["prompt"]
    assert SECRET in github.upsert_sticky_comment.await_args.args[-1]


@pytest.mark.asyncio
async def test_private_pr_text_is_not_published_after_visibility_changes(tmp_path) -> None:
    info = _info("private")
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, _info("public")]),
        list_pull_commit_messages=AsyncMock(return_value=[]),
        upsert_sticky_comment=AsyncMock(),
    )
    payload = {
        "summary": "Change validation",
        "changes": "Update validation logic",
        "linked_task": SECRET,
        "testing": "Not verified",
        "notes_risks": "None",
        "suggested_title": "",
        "title_relevance": "uncertain",
        "title_reason": "",
        "output_language": "en",
    }
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(payload)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=_issue(),
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, mode="comment")),
        settings=Settings.model_construct(openai_api_key="test"),
        visibility="private",
    )
    github.upsert_sticky_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_none_removes_issue_key_from_generated_title_and_suggestion(tmp_path) -> None:
    info = _info()
    info.title = info.head_ref
    payload = {
        "summary": "Change SHA-256 validation",
        "changes": "Keep UTF-8 for CVE-2024 handling",
        "linked_task": "ABC-1",
        "testing": "Not verified",
        "notes_risks": "None",
        "suggested_title": "feat: validate SHA-256 inputs ABC-1",
        "title_relevance": "irrelevant",
        "title_reason": "ABC-1 is not descriptive",
        "output_language": "en",
    }
    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        list_pull_commit_messages=AsyncMock(return_value=["feat: validate inputs"]),
        update_pull_request_title=AsyncMock(),
        upsert_sticky_comment=AsyncMock(),
    )
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(payload)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=_issue(),
        config=AppConfig(
            pr_description=PrDescriptionYaml(enabled=True, mode="comment", title_mode="until_human_edit"),
            public_repos=PublicReposYaml(jira_disclosure="none"),
        ),
        settings=Settings.model_construct(openai_api_key="test"),
        visibility="public",
    )
    assert github.update_pull_request_title.await_args.args[-1] == "feat: validate SHA-256 inputs"
    comment = github.upsert_sticky_comment.await_args.args[-1]
    assert "ABC-1" not in comment
    assert "SHA-256" in comment
    assert "UTF-8" in comment
    assert "CVE-2024" in comment


@pytest.mark.asyncio
async def test_key_only_omits_unavailable_jira_content(tmp_path) -> None:
    info = _info()
    payload = {
        "summary": "Change validation",
        "changes": "Update validation logic",
        "linked_task": SECRET,
        "testing": "Not verified",
        "notes_risks": "None",
        "suggested_title": "",
        "title_relevance": "uncertain",
        "title_reason": "",
        "output_language": "en",
    }
    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        list_pull_commit_messages=AsyncMock(return_value=[]),
        upsert_sticky_comment=AsyncMock(),
    )
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(payload)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, mode="comment")),
        settings=Settings.model_construct(openai_api_key="test"),
        visibility="public",
    )
    text = github.upsert_sticky_comment.await_args.args[-1]
    assert "## Linked task" not in text
    assert SECRET not in text


def test_private_jira_comment_is_unmodified() -> None:
    assert "Scenario:" not in render_jira_comment(_render(visibility="private"))


@pytest.mark.asyncio
async def test_public_main_review_model_context_excludes_jira(monkeypatch) -> None:
    info = _info("public")
    info.title = SECRET
    info.body = plan_pr_body_update("Human notes", SECRET, mode="append") or ""
    repository = SimpleNamespace(full_name="org/repo")
    stored = SimpleNamespace(
        stable_id="prior-1",
        severity="P2",
        title="Existing issue",
        path="src/app.py",
        line=12,
        current_status="open",
        evidence="previous evidence",
        scenario="previous scenario",
        recommendation="previous recommendation",
    )
    pr = SimpleNamespace(id=uuid.uuid4(), repository=repository, number=7, findings=[stored], bot_title=SECRET)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        pull_request=pr,
        status="pending",
        head_sha=info.head_sha,
        summary=None,
        trigger="webhook",
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: run)),
        commit=AsyncMock(),
    )
    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        collaborator_permission=AsyncMock(return_value="write"),
        list_files=AsyncMock(return_value=[]),
    )
    jira = SimpleNamespace(
        enabled=lambda: True, project_allowed=lambda _key: True, get_issue=AsyncMock(return_value=_issue())
    )
    context = SimpleNamespace(
        skip_reason=SKIP_TOO_LARGE,
        files_total=0,
        files_reviewed=0,
        diff_lines=0,
        truncated=True,
        skipped_paths=[],
        prompt_version="test",
    )
    build = Mock(return_value=context)
    monkeypatch.setattr("app.services.orchestrator.build_context", build)
    monkeypatch.setattr("app.services.orchestrator._previous_head", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.orchestrator._persist_snapshot", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr("app.services.orchestrator._publish", publish)
    monkeypatch.setattr("app.services.orchestrator._complete", AsyncMock(return_value=run))

    await run_review(
        session,
        run.id,
        settings=Settings.model_construct(),
        config=AppConfig(),
        github=github,
        jira=jira,
        publisher=SimpleNamespace(),
    )

    assert build.call_args.kwargs["jira_text"] is None
    assert build.call_args.kwargs["title"] == ""
    assert SECRET not in build.call_args.kwargs["body"]
    previous = build.call_args.kwargs["previous_findings"]
    assert len(previous) == 1
    assert previous[0].stable_id == "prior-1"
    assert previous[0].title == ""
    assert previous[0].evidence == ""
    assert publish.await_args.kwargs["visibility"] == "public"
