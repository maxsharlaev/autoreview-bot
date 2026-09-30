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
from app.services.comment_render import (
    FindingTransitionView,
    RenderInput,
    _security_text,
    render_jira_comment,
    render_sticky_comment,
)
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


def test_public_clean_review_keeps_model_summary() -> None:
    render = _render()
    render.verified.summary = "Useful summary of the changes"
    render.verified.findings = []
    text = render_sticky_comment(render)
    assert "Useful summary of the changes" in text
    assert "Review completed. See the findings below." not in text


def test_none_removes_only_linked_jira_key() -> None:
    render = _render(PublicReposYaml(jira_disclosure="none"))
    render.verified.summary = "Use SHA-256 and UTF-8 for CVE-2024, not ABC-1"
    render.verified.findings = []
    text = render_sticky_comment(render)
    assert "ABC-1" not in text
    assert "SHA-256" in text
    assert "UTF-8" in text
    assert "CVE-2024" in text


def test_public_previous_finding_hides_old_private_text() -> None:
    render = _render()
    render.verified.summary = "Review of current changes"
    render.redacted_prior_ids = {"secret-finding"}
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
def test_public_review_redacts_security_findings(disclosure: str) -> None:
    render = _render(PublicReposYaml(jira_disclosure=disclosure))
    text = render_sticky_comment(render)
    assert SECRET not in text
    assert "payload=secret-token" not in text
    assert "Secret token bypass" not in text
    assert "src/auth.py:42" in text
    assert "[P3]" in text
    assert "`partial`" in text
    assert "Potential security issue." in text
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


def _correctness_with_keyword(keyword: str) -> FindingView:
    return FindingView(
        stable_id="keyword-finding",
        severity="P2",
        confidence=0.85,
        category="correctness",
        path="src/handler.py",
        line=55,
        title=f"Unvalidated input could allow {keyword}",
        scenario=f"Attacker exploits {keyword} via query param.",
        evidence="No sanitization before database call.",
        recommendation="Validate and escape the input.",
        blocking_candidate=True,
    )


def _correctness_clean() -> FindingView:
    return FindingView(
        stable_id="clean-finding",
        severity="P2",
        confidence=0.9,
        category="correctness",
        path="src/logic.py",
        line=30,
        title="Null check missing",
        scenario="When user is None, method throws.",
        evidence="get_user() can return None.",
        recommendation="Add an explicit None check.",
        blocking_candidate=True,
    )


def test_keyword_escalation_redacts_correctness_with_injection() -> None:
    render = _render()
    render.verified.findings = [_correctness_with_keyword("SQL injection")]
    render.verified.summary = "Possible SQL injection vulnerability."
    text = render_sticky_comment(render)
    assert "SQL injection" not in text
    assert "Unvalidated input" not in text
    assert "Attacker exploits" not in text
    assert "Potential security issue." in text
    assert "src/handler.py:55" in text
    assert "[P2]" in text
    assert "Review completed. See the findings below." in text


def test_keyword_escalation_redacts_correctness_with_xss() -> None:
    render = _render()
    render.verified.findings = [_correctness_with_keyword("XSS")]
    text = render_sticky_comment(render)
    assert "XSS" not in text
    assert "Potential security issue." in text


def test_keyword_escalation_redacts_correctness_with_path_traversal() -> None:
    render = _render()
    render.verified.findings = [_correctness_with_keyword("path traversal")]
    text = render_sticky_comment(render)
    assert "path traversal" not in text
    assert "Potential security issue." in text


def test_keyword_escalation_redacts_correctness_with_deserialization() -> None:
    render = _render()
    render.verified.findings = [_correctness_with_keyword("deserialization")]
    text = render_sticky_comment(render)
    assert "deserialization" not in text
    assert "Potential security issue." in text


def test_keyword_escalation_case_insensitive() -> None:
    render = _render()
    render.verified.findings = [_correctness_with_keyword("SSRF")]
    text = render_sticky_comment(render)
    assert "SSRF" not in text
    assert "Potential security issue." in text


def test_cwe_escalation_redacts_correctness_with_cwe_reference() -> None:
    finding = FindingView(
        stable_id="cwe-finding",
        severity="P1",
        confidence=0.95,
        category="correctness",
        path="src/api.py",
        line=100,
        title="Buffer size not validated",
        scenario="Heap overflow when input > 4096 bytes (CWE-122).",
        evidence="memcpy uses user-supplied length.",
        recommendation="Validate buffer size before copy.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "CWE-122" not in text
    assert "Buffer size" not in text
    assert "Heap overflow" not in text
    assert "Potential security issue." in text
    assert "src/api.py:100" in text


def test_cwe_escalation_case_insensitive() -> None:
    finding = FindingView(
        stable_id="cwe-lower",
        severity="P2",
        confidence=0.8,
        category="correctness",
        path="src/parse.py",
        line=22,
        title="Format string bug (cwe-134)",
        scenario="User input in printf format.",
        evidence="printf(user_data).",
        recommendation='Use printf("%s", user_data).',
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "cwe-134" not in text
    assert "Potential security issue." in text


def test_non_matching_correctness_finding_visible() -> None:
    render = _render()
    render.verified.findings = [_correctness_clean()]
    render.verified.summary = "Minor correctness issue found."
    text = render_sticky_comment(render)
    assert "Null check missing" in text
    assert "When user is None" in text
    assert "get_user() can return None." in text
    assert "Add an explicit None check." in text
    assert "correctness" in text
    assert "src/logic.py:30" in text
    assert "Minor correctness issue found." in text
    assert "Potential security issue." not in text


def test_redact_all_hides_all_findings() -> None:
    render = _render(PublicReposYaml(security_findings="redact_all"))
    render.verified.findings = [_correctness_clean()]
    render.verified.summary = "Minor correctness issue found."
    text = render_sticky_comment(render)
    assert "Null check missing" not in text
    assert "When user is None" not in text
    assert "Potential security issue." in text
    assert "src/logic.py:30" in text
    assert "Review completed. See the findings below." in text
    assert "Details are hidden because the repository is public." in text


def test_redact_all_summary_hidden_when_findings_present() -> None:
    render = _render(PublicReposYaml(security_findings="redact_all"))
    render.verified.findings = [_correctness_clean()]
    render.verified.summary = "Contains internal discussion about the code."
    text = render_sticky_comment(render)
    assert "internal discussion" not in text
    assert "Review completed. See the findings below." in text


def test_redact_summary_hidden_when_keyword_match() -> None:
    render = _render()
    render.verified.findings = [_correctness_with_keyword("credential")]
    render.verified.summary = "Credential handling needs improvement."
    text = render_sticky_comment(render)
    assert "Credential handling" not in text
    assert "Review completed. See the findings below." in text


def test_redact_summary_visible_when_no_security_content() -> None:
    render = _render()
    render.verified.findings = [_correctness_clean()]
    render.verified.summary = "Minor issues found in error handling."
    text = render_sticky_comment(render)
    assert "Minor issues found in error handling." in text
    assert "Review completed. See the findings below." not in text


def test_keyword_escalation_transition_redacted() -> None:
    render = _render()
    render.verified.findings = []
    render.transitions = [
        FindingTransitionView(
            stable_id="old-injection",
            status="resolved",
            path="src/db.py",
            line=88,
            severity="P1",
            title="SQL injection in search query",
            category="correctness",
        )
    ]
    text = render_sticky_comment(render)
    assert "SQL injection" not in text
    assert "Potential security issue." in text
    assert "src/db.py:88" in text


def test_cwe_escalation_transition_redacted() -> None:
    render = _render()
    render.verified.findings = []
    render.transitions = [
        FindingTransitionView(
            stable_id="old-cwe",
            status="still_open",
            path="src/mem.py",
            line=12,
            severity="P0",
            title="Memory corruption (CWE-787)",
            category="correctness",
        )
    ]
    text = render_sticky_comment(render)
    assert "CWE-787" not in text
    assert "Memory corruption" not in text
    assert "Potential security issue." in text


def test_redact_all_transition_redacted() -> None:
    render = _render(PublicReposYaml(security_findings="redact_all"))
    render.verified.findings = []
    render.transitions = [
        FindingTransitionView(
            stable_id="any-transition",
            status="resolved",
            path="src/utils.py",
            line=5,
            severity="P3",
            title="Style improvement",
            category="style",
        )
    ]
    text = render_sticky_comment(render)
    assert "Style improvement" not in text
    assert "Potential security issue." in text


def test_config_accepts_redact_all() -> None:
    config = PublicReposYaml(security_findings="redact_all")
    assert config.security_findings == "redact_all"


def test_config_accepts_all_security_findings_values() -> None:
    for value in ("redact", "redact_all", "full"):
        config = PublicReposYaml(security_findings=value)
        assert config.security_findings == value


def test_config_rejects_invalid_security_findings() -> None:
    with pytest.raises(ValueError):
        PublicReposYaml(security_findings="invalid")


def test_jira_comment_includes_keyword_escalated_findings() -> None:
    render = _render()
    render.verified.findings = [_correctness_with_keyword("RCE")]
    text = render_jira_comment(render)
    assert "RCE" in text
    assert "Unvalidated input" in text
    assert "src/handler.py:55" in text


def test_jira_comment_includes_cwe_escalated_findings() -> None:
    finding = FindingView(
        stable_id="jira-cwe",
        severity="P1",
        confidence=0.9,
        category="correctness",
        path="src/auth.py",
        line=77,
        title="Auth bypass (CWE-287)",
        scenario="Token validation skipped.",
        evidence="if (bypass) return true.",
        recommendation="Remove bypass flag.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_jira_comment(render)
    assert "CWE-287" in text
    assert "Auth bypass" in text


def test_jira_comment_omits_clean_correctness_in_redact_mode() -> None:
    render = _render()
    render.verified.findings = [_correctness_clean()]
    text = render_jira_comment(render)
    assert "Null check missing" not in text


def test_jira_comment_includes_all_in_redact_all_mode() -> None:
    render = _render(PublicReposYaml(security_findings="redact_all"))
    render.verified.findings = [_correctness_clean()]
    text = render_jira_comment(render)
    assert "Null check missing" in text
    assert "When user is None" in text


def test_leftover_open_keyword_escalated_finding_redacted() -> None:
    render = _render()
    render.verified.findings = []
    render.transitions = [
        FindingTransitionView(
            stable_id="leftover-injection",
            status="still_open",
            path="src/query.py",
            line=33,
            severity="P1",
            title="Command injection in shell call",
            scenario="User input passed to subprocess.",
            evidence="subprocess.run(user_cmd)",
            recommendation="Use subprocess with shell=False.",
            category="correctness",
        )
    ]
    text = render_sticky_comment(render)
    assert "### Still open from previous review" in text
    assert "Command injection" not in text
    assert "User input passed" not in text
    assert "Potential security issue." in text
    assert "src/query.py:33" in text


def test_leftover_open_clean_correctness_visible() -> None:
    render = _render()
    render.verified.findings = []
    render.transitions = [
        FindingTransitionView(
            stable_id="leftover-clean",
            status="still_open",
            path="src/validator.py",
            line=10,
            severity="P2",
            title="Missing range check",
            scenario="Value can exceed max.",
            evidence="No upper bound validation.",
            recommendation="Add max value check.",
            category="correctness",
        )
    ]
    text = render_sticky_comment(render)
    assert "### Still open from previous review" in text
    assert "Missing range check" in text
    assert "Value can exceed max." in text
    assert "No upper bound validation." in text


def test_multiple_keywords_detected() -> None:
    finding = FindingView(
        stable_id="multi-keyword",
        severity="P0",
        confidence=0.99,
        category="data",
        path="src/export.py",
        line=200,
        title="Credential and token exposure",
        scenario="API credentials logged with token leak.",
        evidence="logger.info(f'creds={creds}, token={token}')",
        recommendation="Remove sensitive data from logs.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "Credential" not in text
    assert "token leak" not in text
    assert "Potential security issue." in text


def test_word_boundary_prevents_partial_matches() -> None:
    finding = FindingView(
        stable_id="partial-match",
        severity="P2",
        confidence=0.8,
        category="correctness",
        path="src/session.py",
        line=15,
        title="Session state corruption",
        scenario="The sessionid is lost during redirect.",
        evidence="sessionid cleared before redirect.",
        recommendation="Preserve sessionid across redirects.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "Session state corruption" in text
    assert "sessionid" in text
    assert "Potential security issue." not in text


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


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("Leaks secrets to logs", True),
        ("Stores credentials in plain text", True),
        ("Auth token logged", True),
        ("Hardcoded passwords in config", True),
        ("API keys exposed in environment", True),
        ("Access keys stored unencrypted", True),
        ("Session hijacking possible", True),
        ("Vulnerable to SQLi attack", True),
        ("SQL built from user input", True),
        ("Missing null check in handler", False),
        ("Date parsing fails on leap year", False),
    ],
)
def test_security_text_plural_keywords(phrase: str, expected: bool) -> None:
    assert _security_text(phrase) is expected


def test_plural_keyword_finding_redacted() -> None:
    finding = FindingView(
        stable_id="plural-secrets",
        severity="P1",
        confidence=0.95,
        category="correctness",
        path="src/logger.py",
        line=42,
        title="Leaks secrets to logs",
        scenario="Sensitive secrets logged in production.",
        evidence="logger.info(secrets)",
        recommendation="Remove secrets from log output.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "Leaks secrets" not in text
    assert "Sensitive secrets" not in text
    assert "Potential security issue." in text


def test_plural_keyword_credentials_redacted() -> None:
    finding = FindingView(
        stable_id="plural-creds",
        severity="P1",
        confidence=0.9,
        category="correctness",
        path="src/storage.py",
        line=88,
        title="Stores credentials in plain text",
        scenario="User credentials saved without encryption.",
        evidence="db.save(creds)",
        recommendation="Encrypt credentials at rest.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "credentials" not in text
    assert "Stores credentials" not in text
    assert "Potential security issue." in text


def test_token_keyword_finding_redacted() -> None:
    finding = FindingView(
        stable_id="token-logged",
        severity="P2",
        confidence=0.85,
        category="correctness",
        path="src/middleware.py",
        line=23,
        title="Auth token logged",
        scenario="Bearer token appears in debug logs.",
        evidence="print(f'token={auth_token}')",
        recommendation="Mask token in logs.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "token" not in text.lower() or "Potential security issue" in text
    assert "Bearer token" not in text
    assert "Potential security issue." in text


def test_sql_from_user_input_finding_redacted() -> None:
    finding = FindingView(
        stable_id="sql-style",
        severity="P2",
        confidence=0.8,
        category="correctness",
        path="src/query_builder.py",
        line=55,
        title="SQL built from user input without parameterization",
        scenario="Query concatenates user input directly.",
        evidence="query = f'SELECT * FROM users WHERE id={user_id}'",
        recommendation="Use parameterized queries.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "SQL built from user input" not in text
    assert "Query concatenates" not in text
    assert "Potential security issue." in text


@pytest.mark.parametrize(
    "title,scenario,expected",
    [
        (
            "SQL query built from user input",
            "Query concatenates user-supplied data without parameterization.",
            True,
        ),
        (
            "Shell command from request params",
            "Command built from request parameter value.",
            True,
        ),
        (
            "File path from user-supplied filename",
            "Path constructed from untrusted filename input.",
            True,
        ),
        (
            "Eval of user input",
            "User-controlled string passed to eval().",
            True,
        ),
        (
            "Pickle loads on request body",
            "Request body deserialized via pickle.loads without validation.",
            True,
        ),
        (
            "InnerHTML with user input",
            "User input assigned to innerHTML property.",
            True,
        ),
        (
            "Disabled certificate verification",
            "HTTP client uses verify=False.",
            True,
        ),
        (
            "Off-by-one in pagination",
            "Page index starts at 1 but code uses 0.",
            False,
        ),
        (
            "Null check missing on empty list",
            "Method throws when list is empty.",
            False,
        ),
        (
            "Wrong default timeout",
            "Timeout is 30s but should be 60s per spec.",
            False,
        ),
        (
            "N+1 query in loop",
            "Query executed per item instead of batching.",
            False,
        ),
        (
            "User input form layout",
            "Form fields are misaligned on mobile.",
            False,
        ),
    ],
)
def test_source_sink_pattern_detection(title: str, scenario: str, expected: bool) -> None:
    combined = f"{title} {scenario}"
    assert _security_text(combined) is expected


def test_source_sink_shell_command_redacted() -> None:
    finding = FindingView(
        stable_id="shell-request",
        severity="P1",
        confidence=0.9,
        category="correctness",
        path="src/runner.py",
        line=88,
        title="Shell command from request params",
        scenario="Command built from request parameter value.",
        evidence="subprocess.run(cmd, shell=True)",
        recommendation="Use subprocess with list args, not shell=True.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "Shell command" not in text
    assert "request param" not in text
    assert "Potential security issue." in text


def test_source_sink_eval_user_input_redacted() -> None:
    finding = FindingView(
        stable_id="eval-user",
        severity="P0",
        confidence=0.95,
        category="correctness",
        path="src/calc.py",
        line=12,
        title="Eval of user input",
        scenario="User-controlled string passed to eval().",
        evidence="result = eval(user_expr)",
        recommendation="Use a safe expression parser.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "Eval of user input" not in text
    assert "eval()" not in text
    assert "Potential security issue." in text


def test_source_sink_pickle_request_redacted() -> None:
    finding = FindingView(
        stable_id="pickle-request",
        severity="P0",
        confidence=0.99,
        category="correctness",
        path="src/api.py",
        line=45,
        title="Pickle loads on request body",
        scenario="Request body deserialized via pickle.loads without validation.",
        evidence="data = pickle.loads(request.body)",
        recommendation="Use JSON or a safe deserializer.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "Pickle loads" not in text
    assert "pickle.loads" not in text
    assert "Potential security issue." in text


def test_source_sink_innerhtml_user_redacted() -> None:
    finding = FindingView(
        stable_id="innerhtml-user",
        severity="P1",
        confidence=0.9,
        category="correctness",
        path="src/components/Widget.jsx",
        line=33,
        title="InnerHTML with user input",
        scenario="User input assigned to innerHTML property.",
        evidence="el.innerHTML = userContent",
        recommendation="Use textContent or sanitize HTML.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "InnerHTML" not in text
    assert "user input" not in text
    assert "Potential security issue." in text


def test_standalone_verify_false_redacted() -> None:
    finding = FindingView(
        stable_id="verify-false",
        severity="P2",
        confidence=0.85,
        category="correctness",
        path="src/client.py",
        line=22,
        title="Disabled certificate verification",
        scenario="HTTP client uses verify=False.",
        evidence="requests.get(url, verify=False)",
        recommendation="Enable TLS verification.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "verify=False" not in text
    assert "Potential security issue." in text


def test_non_security_nplus1_query_visible() -> None:
    finding = FindingView(
        stable_id="nplus1",
        severity="P2",
        confidence=0.8,
        category="correctness",
        path="src/repo.py",
        line=100,
        title="N+1 query in loop",
        scenario="Query executed per item instead of batching.",
        evidence="for item in items: db.query(item.id)",
        recommendation="Batch the query outside the loop.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "N+1 query in loop" in text
    assert "Query executed per item" in text
    assert "Potential security issue." not in text


def test_non_security_off_by_one_visible() -> None:
    finding = FindingView(
        stable_id="off-by-one",
        severity="P2",
        confidence=0.9,
        category="correctness",
        path="src/pagination.py",
        line=55,
        title="Off-by-one in pagination",
        scenario="Page index starts at 1 but code uses 0.",
        evidence="page = params.get('page', 0)",
        recommendation="Default to 1 for user-facing pages.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "Off-by-one in pagination" in text
    assert "Page index starts at 1" in text
    assert "Potential security issue." not in text


def test_non_security_form_layout_visible() -> None:
    finding = FindingView(
        stable_id="form-layout",
        severity="P3",
        confidence=0.7,
        category="correctness",
        path="src/components/Form.jsx",
        line=88,
        title="User input form layout",
        scenario="Form fields are misaligned on mobile.",
        evidence="flexDirection: row does not wrap.",
        recommendation="Use flex-wrap or stack on mobile.",
        blocking_candidate=False,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_sticky_comment(render)
    assert "User input form layout" in text
    assert "Form fields are misaligned" in text
    assert "Potential security issue." not in text


def test_redact_all_always_replaces_summary() -> None:
    render = _render(PublicReposYaml(security_findings="redact_all"))
    render.verified.findings = []
    render.transitions = []
    render.verified.summary = "All clear, no issues found."
    text = render_sticky_comment(render)
    assert "All clear" not in text
    assert "Review completed. See the findings below." in text


def test_redact_all_empty_findings_uses_public_summary() -> None:
    render = _render(PublicReposYaml(security_findings="redact_all"))
    render.verified.findings = []
    render.transitions = []
    render.verified.summary = "Clean review with detailed internal notes."
    text = render_sticky_comment(render)
    assert "internal notes" not in text
    assert "Review completed. See the findings below." in text


def test_redact_mode_leaking_summary_replaced() -> None:
    render = _render()
    render.verified.findings = []
    render.transitions = []
    render.verified.summary = "User input passed to SQL query without parameterization."
    text = render_sticky_comment(render)
    assert "SQL query" not in text
    assert "user input" not in text.lower()
    assert "Review completed. See the findings below." in text


def test_redact_mode_clean_summary_visible() -> None:
    render = _render()
    render.verified.findings = []
    render.transitions = []
    render.verified.summary = "No issues found in the pagination logic."
    text = render_sticky_comment(render)
    assert "No issues found in the pagination logic." in text
    assert "Review completed. See the findings below." not in text


def test_jira_routes_source_sink_finding() -> None:
    finding = FindingView(
        stable_id="jira-source-sink",
        severity="P1",
        confidence=0.9,
        category="correctness",
        path="src/handler.py",
        line=44,
        title="Shell command from request params",
        scenario="Command built from request parameter value.",
        evidence="subprocess.run(cmd, shell=True)",
        recommendation="Use list args.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_jira_comment(render)
    assert "Shell command from request params" in text
    assert "subprocess.run" in text


def test_jira_routes_standalone_risk_finding() -> None:
    finding = FindingView(
        stable_id="jira-verify-false",
        severity="P2",
        confidence=0.85,
        category="correctness",
        path="src/client.py",
        line=22,
        title="TLS verification disabled",
        scenario="Client uses verify=False for HTTPS.",
        evidence="requests.get(url, verify=False)",
        recommendation="Enable verification.",
        blocking_candidate=True,
    )
    render = _render()
    render.verified.findings = [finding]
    text = render_jira_comment(render)
    assert "verify=False" in text
    assert "TLS verification disabled" in text
