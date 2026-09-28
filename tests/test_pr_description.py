from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest
from app.adapters.github import ChangedFile, GitHubError, PullRequestInfo
from app.adapters.jira import JiraIssue
from app.config import AppConfig, LanguageYaml, PrDescriptionYaml, PrTextYaml, Settings, load_yaml_config
from app.services.orchestrator import _publish_pr_description
from app.services.pr_description import (
    BLOCK_END,
    COMMENT_MARKER,
    build_pr_description_context,
    description_comment_is_intact,
    format_commit_summary,
    plan_pr_body_update,
    plan_pr_title_update,
    render_pr_description,
    render_pr_description_comment,
    render_title_relevance_note,
    title_is_invalid,
    title_source_hash,
    validate_pr_description,
    without_managed_block,
)

PAYLOAD = {
    "summary": "Add validation",
    "changes": "Validate inputs",
    "linked_task": "ABC-1 acceptance criteria covered",
    "testing": "Tests were not run",
    "notes_risks": "Check migration",
    "suggested_title": "feat: validate form inputs",
    "title_relevance": "irrelevant",
    "title_reason": "The title is a placeholder; commits describe form validation.",
    "output_language": "en",
}


def test_feature_is_off_by_default_and_modes_are_validated() -> None:
    assert load_yaml_config(Path("config.example.yaml")).pr_description.enabled is False
    assert load_yaml_config(Path("config.example.yaml")).pr_description.title_mode == "off"
    with pytest.raises(ValueError):
        PrDescriptionYaml(mode="overwrite")
    with pytest.raises(ValueError):
        PrDescriptionYaml(title_mode="sometimes")
    assert PrDescriptionYaml.model_validate({"title_mode": False}).title_mode == "off"
    assert PrDescriptionYaml(title_mode="always").title_mode == "until_human_edit"


def test_legacy_always_mode_emits_warning(caplog) -> None:
    with caplog.at_level("WARNING"):
        assert PrDescriptionYaml(title_mode="always").title_mode == "until_human_edit"
    assert "deprecated" in caplog.text


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
    assert "<untrusted_commit_messages>\ntotal_commits: 1\ncommits_truncated: false" in prompt
    assert "feat: add form validation" in prompt
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
    assert "\\@alice" in draft
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


def test_title_modes_and_relevance_guard() -> None:
    assert title_is_invalid("Dev", "feature/form")
    assert title_is_invalid("feature/form", "feature/form")
    assert title_is_invalid("Validate form inputs", "feature/form")
    assert not title_is_invalid("feat: validate form inputs", "feature/form")
    options = dict(head_ref="feature/form", check_relevance=True)
    suggestion = "feat: validate form inputs"
    assert plan_pr_title_update("Dev", suggestion, mode="off", relevance="irrelevant", **options) is None
    assert plan_pr_title_update("Dev", suggestion, mode="until_human_edit", relevance="irrelevant", **options) is None
    assert (
        plan_pr_title_update(
            "feature/form", suggestion, mode="when_invalid_or_inconsistent", relevance="uncertain", **options
        )
        == suggestion
    )
    assert (
        plan_pr_title_update(
            "feat: improve logging",
            suggestion,
            mode="when_invalid_or_inconsistent",
            relevance="irrelevant",
            bot_title="feat: improve logging",
            **options,
        )
        == suggestion
    )
    assert (
        plan_pr_title_update(
            "Improve logging", suggestion, mode="when_invalid_or_inconsistent", relevance="uncertain", **options
        )
        is None
    )
    assert (
        plan_pr_title_update("Improve logging", suggestion, mode="until_human_edit", relevance="uncertain", **options)
        is None
    )
    assert (
        plan_pr_title_update("feature/form", suggestion, mode="until_human_edit", relevance="uncertain", **options)
        == suggestion
    )
    assert (
        plan_pr_title_update(
            "feature/form", "feat: @team review", mode="until_human_edit", relevance="irrelevant", **options
        )
        is None
    )
    assert "## Title check" in render_title_relevance_note("Mismatch", language="en")


def test_github_branch_derived_title_can_be_replaced() -> None:
    assert (
        plan_pr_title_update(
            "Feat/3 pr description",
            "feat: generate PR description",
            head_ref="feat/3-pr-description",
            mode="until_human_edit",
            check_relevance=True,
            relevance="uncertain",
        )
        == "feat: generate PR description"
    )


def test_bot_title_is_not_rewritten_without_new_source() -> None:
    assert (
        plan_pr_title_update(
            "feat: old subject",
            "feat: new subject",
            head_ref="feature/a",
            mode="until_human_edit",
            check_relevance=True,
            relevance="irrelevant",
            bot_title="feat: old subject",
            source_unchanged=True,
        )
        is None
    )
    base = dict(head_sha="a" * 40, commits=["feat: old"], files=[], jira_issue=None, human_body="Human note")
    assert title_source_hash(**base) == title_source_hash(**base)
    assert title_source_hash(**base) != title_source_hash(**(base | {"commits": ["feat: new"]}))


def test_title_requires_conventional_format_and_preserves_jira_key() -> None:
    options = dict(head_ref="ABC-7-work", mode="until_human_edit", check_relevance=True, relevance="irrelevant")
    assert plan_pr_title_update("ABC-7-work", "feat: validate forms", **options) == "feat: validate forms ABC-7"
    assert plan_pr_title_update("ABC-7-work", "feat: validate forms XYZ-9", **options) is None
    assert plan_pr_title_update("ABC-7-work", "Validate forms", **options) is None
    assert plan_pr_title_update("ABC-7-work", "feat: see example.com", **options) is None
    assert plan_pr_title_update("ABC-7-work", "feat: see example.technology", **options) is None
    assert plan_pr_title_update("ABC-7-work", "feat: see www.example", **options) is None
    assert (
        plan_pr_title_update("ABC-7-work", "feat: val\u202e\u200bidate forms", **options)
        == "feat: validate forms ABC-7"
    )
    assert title_is_invalid("Validate forms", "feature/forms")
    assert not title_is_invalid("feat: validate forms", "feature/forms")


def test_empty_sections_and_bare_urls_are_not_published_as_links() -> None:
    payload = PAYLOAD | {"notes_risks": "", "changes": "See https://example.com or www.example.com"}
    draft = render_pr_description(payload, language="en", linked_task=False)
    assert "## Notes / risks" not in draft
    assert "https://" not in draft
    assert "www.example.com" not in draft
    assert "&#58;" in draft and "&#46;" in draft


def test_human_title_note_can_include_safe_suggestion() -> None:
    note = render_title_relevance_note("The title is vague", language="en", suggested_title="feat: validate inputs")
    assert "Suggested title: feat: validate inputs" in note


def test_commit_list_truncation_is_explicit() -> None:
    prompt = build_pr_description_context(
        title="Title",
        body="",
        head_ref="feature/a",
        base_sha="b" * 40,
        head_sha="a" * 40,
        files=[],
        commit_messages=["feat: first", "feat: second", "feat: third"],
        jira_issue=None,
        language="en",
        max_commit_messages=2,
    )
    assert "total_commits: 3" in prompt
    assert "commits_truncated: true" in prompt
    assert "feat: first" not in prompt
    assert "feat: third" in prompt
    limited_by_github = build_pr_description_context(
        title="Title",
        body="",
        head_ref="feature/a",
        base_sha="b" * 40,
        head_sha="a" * 40,
        files=[],
        commit_messages=["feat: available"],
        jira_issue=None,
        language="en",
        commit_total=300,
    )
    assert "total_commits: 300" in limited_by_github
    assert "commits_truncated: true" in limited_by_github


def test_commit_budget_filters_noise_groups_subjects_and_reports_omissions() -> None:
    summary = format_commit_summary(
        [
            "fixup! feat: old change",
            "feat: add form\nprivate body text",
            "squash! feat: merge",
            "fix: reject bad values",
            "Merge branch main",
            "WIP temporary",
        ],
        total=8,
        max_chars=28,
    )
    assert "fix:\n- fix: reject bad values" in summary
    assert "private body text" not in summary
    assert "fixup!" not in summary and "squash!" not in summary
    assert "7 more commits not shown" in summary


def test_pr_description_language_key_takes_precedence() -> None:
    from app.services.pr_text_language import resolve_pr_language

    config = AppConfig(pr_description=PrDescriptionYaml(language="fr"), pr_text=PrTextYaml(language="ru"))
    assert resolve_pr_language(config, human_title="Dev", human_body="", commits=[]) == "fr"


def test_fill_empty_updates_only_intact_generated_body() -> None:
    first = plan_pr_body_update("", "first draft", mode="fill_empty")
    assert first is not None and BLOCK_END in first
    second = plan_pr_body_update(first, "second draft", mode="fill_empty")
    assert second is not None and "second draft" in second and "first draft" not in second
    assert second.count(BLOCK_END) == 1
    assert plan_pr_body_update(first.replace("first draft", "author edit"), "new", mode="fill_empty") is None
    assert plan_pr_body_update(first + "\nAuthor note", "new", mode="fill_empty") is None


def test_crlf_managed_block_is_updated_once() -> None:
    first = plan_pr_body_update("Human notes", "first draft", mode="append")
    assert first is not None
    crlf_body = first.replace("\n", "\r\n")
    updated = plan_pr_body_update(crlf_body, "second draft", mode="append")
    assert updated is not None
    assert updated.count(BLOCK_END) == 1
    assert "first draft" not in updated
    assert updated.startswith("Human notes\n\n")


def test_managed_block_is_removed_before_jira_key_search() -> None:
    from app.services.issue_key import extract_issue_key

    body = plan_pr_body_update("Human notes", "fake issue ABC-999", mode="append")
    assert body is not None
    assert extract_issue_key("No key", "feature/no-key", without_managed_block(body)) is None
    assert extract_issue_key("ABC-2: Human title", "feature/ABC-3", without_managed_block(body)) == "ABC-2"


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
@pytest.mark.parametrize("field,value", [("draft", True), ("is_fork", True)])
async def test_draft_and_fork_prs_do_not_generate_text(tmp_path, field: str, value: bool) -> None:
    info = _info()
    setattr(info, field, value)
    github = SimpleNamespace(get_pull_request=AsyncMock(return_value=info))
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


@pytest.mark.asyncio
async def test_invalid_title_is_replaced_without_title_warning(tmp_path) -> None:
    info = _info()
    info.title = "Dev"
    pr = SimpleNamespace(bot_title="Dev", bot_title_source_hash=None, title="Dev")
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info, info]),
        list_pull_commit_messages=AsyncMock(return_value=["feat: validate form inputs"]),
        update_pull_request_title=AsyncMock(),
        upsert_sticky_comment=AsyncMock(),
    )
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(PAYLOAD)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, title_mode="when_invalid_or_inconsistent")),
        settings=Settings.model_construct(openai_api_key="test"),
        pr=pr,
    )
    assert github.update_pull_request_title.await_args.args[-1] == "feat: validate form inputs"
    assert pr.bot_title == "feat: validate form inputs"
    assert "## Title check" not in github.upsert_sticky_comment.await_args.args[-1]


@pytest.mark.asyncio
async def test_title_update_failure_still_posts_description(tmp_path) -> None:
    info = _info()
    info.title = "Dev"
    pr = SimpleNamespace(bot_title="Dev", bot_title_source_hash=None, title="Dev")
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info, info]),
        list_pull_commit_messages=AsyncMock(return_value=["feat: validate form inputs"]),
        update_pull_request_title=AsyncMock(side_effect=GitHubError("PATCH failed")),
        upsert_sticky_comment=AsyncMock(),
    )
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(PAYLOAD)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, title_mode="until_human_edit")),
        settings=Settings.model_construct(openai_api_key="test"),
        pr=pr,
    )
    assert "## Title check" in github.upsert_sticky_comment.await_args.args[-1]


@pytest.mark.asyncio
async def test_bot_title_is_committed_before_description_update(tmp_path) -> None:
    info = _info(body="Author notes")
    info.title = info.head_ref
    pr = SimpleNamespace(bot_title=None, bot_title_source_hash=None, title=info.title)
    session = SimpleNamespace(commit=AsyncMock())

    async def fail_body_update(*_args, **_kwargs) -> None:
        session.commit.assert_awaited_once()
        raise GitHubError("body update failed")

    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        list_pull_commit_messages=AsyncMock(return_value=["feat: validate form inputs"]),
        update_pull_request_title=AsyncMock(),
        update_pull_request_body=AsyncMock(side_effect=fail_body_update),
    )
    with pytest.raises(GitHubError, match="body update failed"):
        await _publish_pr_description(
            session=session,
            github=github,
            codex_fn=AsyncMock(return_value=json.dumps(PAYLOAD)),
            checkout=tmp_path,
            info=info,
            files=[],
            jira_issue=None,
            config=AppConfig(
                pr_description=PrDescriptionYaml(enabled=True, mode="append", title_mode="until_human_edit")
            ),
            settings=Settings.model_construct(openai_api_key="test"),
            pr=pr,
        )
    assert pr.bot_title == "feat: validate form inputs ABC-1"
    assert pr.bot_title_source_hash is not None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_human_title_is_preserved_and_suggestion_is_visible(tmp_path) -> None:
    info = _info()
    info.title = "Dev"
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info]),
        list_pull_commit_messages=AsyncMock(return_value=["feat: validate form inputs"]),
        update_pull_request_title=AsyncMock(),
        upsert_sticky_comment=AsyncMock(),
    )
    await _publish_pr_description(
        github=github,
        codex_fn=AsyncMock(return_value=json.dumps(PAYLOAD)),
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_description=PrDescriptionYaml(enabled=True, title_mode="when_invalid_or_inconsistent")),
        settings=Settings.model_construct(openai_api_key="test"),
        pr=SimpleNamespace(bot_title=None, bot_title_source_hash=None, title="Dev"),
    )
    github.update_pull_request_title.assert_not_awaited()
    assert "Suggested title: feat: validate form inputs" in github.upsert_sticky_comment.await_args.args[-1]


@pytest.mark.asyncio
async def test_wrong_language_retries_once_before_publication(tmp_path) -> None:
    info = _info()
    info.title = "Проверить входные данные"
    russian = PAYLOAD | {
        "summary": "Проверка входных данных",
        "changes": "Добавлена проверка формы",
        "testing": "Тесты не запускались",
        "notes_risks": "Риски не выявлены",
        "suggested_title": "feat(form): Проверить входные данные",
        "title_reason": "Заголовок соответствует коммитам",
        "output_language": "ru",
    }
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info]),
        list_pull_commit_messages=AsyncMock(return_value=["feat: Проверить входные данные"]),
        upsert_sticky_comment=AsyncMock(),
    )
    codex = AsyncMock(side_effect=[json.dumps(PAYLOAD), json.dumps(russian)])
    await _publish_pr_description(
        github=github,
        codex_fn=codex,
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_text=PrTextYaml(language="auto"), pr_description=PrDescriptionYaml(enabled=True)),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    assert codex.await_count == 2
    assert "## Кратко" in github.upsert_sticky_comment.await_args.args[-1]


@pytest.mark.asyncio
async def test_two_wrong_language_outputs_do_not_publish(tmp_path) -> None:
    info = _info()
    info.title = "Проверить входные данные"
    github = SimpleNamespace(
        get_pull_request=AsyncMock(return_value=info),
        list_pull_commit_messages=AsyncMock(return_value=[]),
        upsert_sticky_comment=AsyncMock(),
    )
    codex = AsyncMock(return_value=json.dumps(PAYLOAD))
    await _publish_pr_description(
        github=github,
        codex_fn=codex,
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_text=PrTextYaml(language="auto"), pr_description=PrDescriptionYaml(enabled=True)),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    assert codex.await_count == 2
    github.upsert_sticky_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_language_uses_independent_check_and_retries(tmp_path) -> None:
    info = _info()
    french = PAYLOAD | {
        "summary": "Vérifier les données saisies",
        "changes": "Ajouter une validation du formulaire",
        "testing": "Les tests ne sont pas exécutés",
        "notes_risks": "Aucun risque identifié",
        "suggested_title": "feat(form): vérifier les données saisies",
        "title_reason": "Le titre ne décrit pas les commits",
        "output_language": "fr",
    }
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=[info, info]),
        list_pull_commit_messages=AsyncMock(return_value=["feat: validate form inputs"]),
        upsert_sticky_comment=AsyncMock(),
    )
    codex = AsyncMock(
        side_effect=[
            json.dumps(PAYLOAD | {"output_language": "fr"}),
            json.dumps({"matches": False}),
            json.dumps(french),
            json.dumps({"matches": True}),
        ]
    )
    await _publish_pr_description(
        github=github,
        codex_fn=codex,
        checkout=tmp_path,
        info=info,
        files=[],
        jira_issue=None,
        config=AppConfig(pr_text=PrTextYaml(language="fr"), pr_description=PrDescriptionYaml(enabled=True)),
        settings=Settings.model_construct(openai_api_key="test"),
    )
    assert codex.await_count == 4
    assert "language code fr" in codex.await_args_list[0].kwargs["prompt"]
    assert "predominantly in language code fr" in codex.await_args_list[1].kwargs["prompt"]
    assert "Vérifier les données" in github.upsert_sticky_comment.await_args.args[-1]
