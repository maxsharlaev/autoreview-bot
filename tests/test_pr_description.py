from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest
from app.adapters.github import ChangedFile, PullRequestInfo
from app.adapters.jira import JiraIssue
from app.config import AppConfig, LanguageYaml, PrDescriptionYaml, Settings, load_yaml_config
from app.services.orchestrator import _publish_pr_description
from app.services.pr_description import (
    BLOCK_END,
    COMMENT_MARKER,
    build_pr_description_context,
    description_comment_is_intact,
    plan_pr_body_update,
    render_pr_description,
    render_pr_description_comment,
    validate_pr_description,
)

PAYLOAD = {
    "summary": "Add validation",
    "changes": "Validate inputs",
    "linked_task": "ABC-1 acceptance criteria covered",
    "testing": "Tests were not run",
    "notes_risks": "Check migration",
}


def test_feature_is_off_by_default_and_modes_are_validated() -> None:
    assert load_yaml_config(Path("config.example.yaml")).pr_description.enabled is False
    with pytest.raises(ValueError):
        PrDescriptionYaml(mode="overwrite")


def _issue() -> JiraIssue:
    return JiraIssue(
        key="ABC-1",
        summary="Validate inputs",
        description="Reject bad values",
        status="In Progress",
        issue_type="Task",
        priority="Medium",
        acceptance_criteria="Invalid values return 400",
    )


def _info(body: str = "", sha: str = "a" * 40) -> PullRequestInfo:
    return PullRequestInfo(
        number=7,
        title="Validate form",
        body=body,
        html_url="https://github.com/org/repo/pull/7",
        author="author",
        assignee=None,
        state="open",
        draft=False,
        is_fork=False,
        base_sha="b" * 40,
        head_sha=sha,
        head_ref="ABC-1-validation",
        base_ref="main",
        owner="org",
        repo="repo",
        full_name="org/repo",
    )


def test_context_marks_inputs_untrusted_and_uses_locale() -> None:
    prompt = build_pr_description_context(
        title="Validate form",
        body="Author notes",
        head_ref="ABC-1-validation",
        base_sha="b" * 40,
        head_sha="a" * 40,
        files=[ChangedFile(path="app/form.py", status="modified", patch="", additions=12, deletions=3)],
        commit_messages=["feat: add form validation"],
        jira_issue=_issue(),
        language="ru",
    )
    assert "Use Russian for all output fields" in prompt
    assert "<untrusted_commit_messages>\nfeat: add form validation" in prompt
    assert "app: 1 files, +12/-3" in prompt
    assert "Invalid values return 400" in prompt
    assert "<untrusted_pr_body>\nAuthor notes" in prompt


def test_custom_prompt_file_and_missing_file(tmp_path: Path) -> None:
    custom = tmp_path / "draft.md"
    custom.write_text("Draft in {{details_language}}.", encoding="utf-8")
    fields = dict(
        title="Title",
        body="",
        head_ref="feature/x",
        base_sha="b" * 40,
        head_sha="a" * 40,
        files=[],
        commit_messages=[],
        jira_issue=None,
        language="en",
    )
    assert "Draft in English." in build_pr_description_context(**fields, prompt_file=str(custom))
    with pytest.raises(FileNotFoundError, match="PR description prompt file does not exist"):
        build_pr_description_context(**fields, prompt_file=str(tmp_path / "missing.md"))


def test_no_jira_omits_linked_task_and_escapes_markup() -> None:
    payload = PAYLOAD | {"changes": "<script>alert(1)</script> @alice [link](https://example.com)"}
    draft = render_pr_description(payload, language="en", linked_task=False)
    assert "## Linked task" not in draft
    assert "<script>" not in draft
    assert "&lt;script&gt;" in draft
    assert "@\u200balice" in draft
    assert "[link](" not in draft
    assert render_pr_description_comment(draft).startswith(COMMENT_MARKER)
    assert description_comment_is_intact(render_pr_description_comment(draft))
    assert not description_comment_is_intact(render_pr_description_comment(draft) + "edited")


def test_schema_rejects_extra_fields_and_invalid_types() -> None:
    assert validate_pr_description(PAYLOAD) == PAYLOAD
    with pytest.raises(jsonschema.ValidationError):
        validate_pr_description(PAYLOAD | {"html": "<hidden>"})
    with pytest.raises(jsonschema.ValidationError):
        validate_pr_description(PAYLOAD | {"testing": ["not a string"]})
    with pytest.raises(jsonschema.ValidationError):
        validate_pr_description(PAYLOAD | {"summary": "   "})


def test_fill_empty_updates_only_intact_generated_body() -> None:
    first = plan_pr_body_update("", "first draft", mode="fill_empty")
    assert first is not None and BLOCK_END in first
    second = plan_pr_body_update(first, "second draft", mode="fill_empty")
    assert second is not None and "second draft" in second and "first draft" not in second
    assert second.count(BLOCK_END) == 1
    assert plan_pr_body_update(first.replace("first draft", "author edit"), "new", mode="fill_empty") is None
    assert plan_pr_body_update(first + "\nAuthor note", "new", mode="fill_empty") is None


def test_fill_empty_accepts_only_unchanged_template() -> None:
    template = "## Summary\n\nPlease describe your change."
    assert plan_pr_body_update(template, "draft", mode="fill_empty", template=template) is not None
    assert plan_pr_body_update(template + " Added by author", "draft", mode="fill_empty", template=template) is None
    assert plan_pr_body_update("Author description", "draft", mode="fill_empty") is None


def test_append_preserves_author_text_and_updates_one_block() -> None:
    first = plan_pr_body_update("Author text", "first draft", mode="append")
    assert first is not None and first.startswith("Author text\n\n")
    second = plan_pr_body_update(first, "second draft", mode="append")
    assert second is not None and second.startswith("Author text\n\n")
    assert second.count(BLOCK_END) == 1
    assert "first draft" not in second
    assert plan_pr_body_update(first.replace("first draft", "author edit"), "new", mode="append") is None
    assert (
        plan_pr_body_update("Author <!-- autoreview-bot:pr-description:start sha256=bad -->", "new", mode="append")
        is None
    )


@pytest.mark.asyncio
async def test_orchestrator_updates_body_without_jira(tmp_path) -> None:
    info = _info("Author text")
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info, info]),
        list_pull_commit_messages=AsyncMock(return_value=["feat: add validation"]),
        update_pull_request_body=AsyncMock(),
        upsert_sticky_comment=AsyncMock(),
    )
    codex = AsyncMock(return_value=json.dumps(PAYLOAD))
    config = AppConfig(pr_description=PrDescriptionYaml(enabled=True, mode="append"), language=LanguageYaml())
    await _publish_pr_description(
        github=github,
        codex_fn=codex,
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=config,
        settings=Settings.model_construct(openai_api_key="test"),
    )
    body = github.update_pull_request_body.await_args.args[3]
    assert body.startswith("Author text")
    assert "## Linked task" not in body
    assert BLOCK_END in body
    assert "<untrusted_commit_messages>" in codex.await_args.kwargs["prompt"]
    assert codex.await_args.kwargs["sandbox"] == "read-only"
    assert codex.await_args.kwargs["approval_policy"] == "never"
    github.upsert_sticky_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_head_never_publishes(tmp_path) -> None:
    info = _info()
    github = SimpleNamespace(get_pull_request=AsyncMock(return_value=_info(sha="c" * 40)))
    codex = AsyncMock()
    await _publish_pr_description(
        github=github,
        codex_fn=codex,
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True)),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    codex.assert_not_awaited()


@pytest.mark.asyncio
async def test_author_edit_before_publish_is_preserved(tmp_path) -> None:
    info = _info("Author text")
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info, _info("Author changed it")]),
        list_pull_commit_messages=AsyncMock(return_value=[]),
        update_pull_request_body=AsyncMock(),
    )
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(PAYLOAD)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, mode="append")),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    github.update_pull_request_body.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_mode_uses_separate_sticky_comment(tmp_path) -> None:
    info = _info("Human description")
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info]),
        list_pull_commit_messages=AsyncMock(return_value=[]),
        upsert_sticky_comment=AsyncMock(),
        update_pull_request_body=AsyncMock(),
    )
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(PAYLOAD)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, mode="comment")),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    assert github.upsert_sticky_comment.await_args.kwargs["marker"] == COMMENT_MARKER
    github.update_pull_request_body.assert_not_awaited()
