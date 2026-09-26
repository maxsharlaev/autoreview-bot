from pathlib import Path

import pytest
from app.adapters.github import ChangedFile
from app.adapters.jira import adf_to_text
from app.config import LanguageYaml, load_yaml_config
from app.services.constants import SKIP_TOO_LARGE
from app.services.context_builder import PreviousFinding, build_context
from pydantic import ValidationError


def test_load_example_yaml() -> None:
    config = load_yaml_config(Path("config.example.yaml"))
    assert config.slack.enabled is False
    assert config.language.summary == "en"
    assert config.language.details == "en"
    assert config.jira.projects["ABC"].rework_status == "In Progress"
    assert config.codex.model == "gpt-5.6-sol"
    assert config.codex.reasoning_effort == "medium"
    assert config.codex.sandbox == "read-only"
    assert config.codex.approval_policy == "never"


def test_language_rejects_unsupported_locale() -> None:
    with pytest.raises(ValidationError):
        LanguageYaml(details="unsupported")


def test_context_uses_configured_languages() -> None:
    fields = dict(
        repository="org/repo",
        pr_number=1,
        title="t",
        body="b",
        head_ref="fix/x",
        base_sha="b" * 40,
        head_sha="a" * 40,
        previous_head_sha=None,
        files=[],
        issue_key=None,
        jira_text=None,
        jira_warning=None,
        previous_findings=[],
        limits=load_yaml_config(Path("config.example.yaml")).codex,
    )
    english = build_context(**fields).prompt
    russian = build_context(**fields, language=LanguageYaml(summary="ru", details="ru")).prompt
    mixed = build_context(**fields, language=LanguageYaml(summary="en", details="ru")).prompt
    assert "Write `summary` and finding `title` in English." in english
    assert "Write finding `scenario`, `evidence`, and `recommendation` in English." in english
    assert "Write `summary` and finding `title` in Russian." in russian
    assert "Write finding `scenario`, `evidence`, and `recommendation` in Russian." in mixed
    assert "{{summary_language}}" not in english


def test_context_uses_custom_prompt_file(tmp_path: Path) -> None:
    prompt_file = tmp_path / "review.md"
    prompt_file.write_text("Custom review in {{summary_language}} and {{details_language}}.", encoding="utf-8")
    limits = load_yaml_config(Path("config.example.yaml")).codex.model_copy(update={"prompt_file": str(prompt_file)})
    context = build_context(
        repository="org/repo",
        pr_number=1,
        title="t",
        body="b",
        head_ref="fix/x",
        base_sha="b" * 40,
        head_sha="a" * 40,
        previous_head_sha=None,
        files=[],
        issue_key=None,
        jira_text=None,
        jira_warning=None,
        previous_findings=[],
        limits=limits,
        language=LanguageYaml(summary="en", details="ru"),
    )
    assert "Custom review in English and Russian." in context.prompt
    assert "You are the Open PR Review agent." not in context.prompt
    assert context.prompt_version.startswith("custom-")
    assert len(context.prompt_version) == 23


def test_context_rejects_missing_custom_prompt(tmp_path: Path) -> None:
    limits = load_yaml_config(Path("config.example.yaml")).codex.model_copy(
        update={"prompt_file": str(tmp_path / "missing.md")}
    )
    with pytest.raises(FileNotFoundError, match="Review prompt file does not exist"):
        build_context(
            repository="org/repo",
            pr_number=1,
            title="t",
            body="b",
            head_ref="fix/x",
            base_sha="b" * 40,
            head_sha="a" * 40,
            previous_head_sha=None,
            files=[],
            issue_key=None,
            jira_text=None,
            jira_warning=None,
            previous_findings=[],
            limits=limits,
        )


def test_adf_to_text() -> None:
    node = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}],
    }
    assert "Hello" in adf_to_text(node)


def test_context_skips_oversized_pr() -> None:
    files = [
        ChangedFile(path=f"file_{index}.py", status="modified", patch="+" + ("x\n" * 300), additions=300, deletions=0)
        for index in range(5)
    ]
    limits = load_yaml_config(Path("config.example.yaml")).codex.model_copy(
        update={"max_files": 2, "max_diff_lines": 10}
    )
    context = build_context(
        repository="org/repo",
        pr_number=1,
        title="t",
        body="b",
        head_ref="ABC-1",
        base_sha="b" * 40,
        head_sha="a" * 40,
        previous_head_sha=None,
        files=files,
        issue_key="ABC-1",
        jira_text=None,
        jira_warning=None,
        previous_findings=[],
        limits=limits,
    )
    assert context.skip_reason == SKIP_TOO_LARGE
    assert context.files_reviewed == 0
    assert "<untrusted_pr_title>" in context.prompt


def test_context_includes_previous_finding_body() -> None:
    limits = load_yaml_config(Path("config.example.yaml")).codex
    context = build_context(
        repository="org/repo",
        pr_number=1,
        title="t",
        body="b",
        head_ref="fix/x",
        base_sha="b" * 40,
        head_sha="a" * 40,
        previous_head_sha="c" * 40,
        files=[ChangedFile(path="a.py", status="modified", patch="+x", additions=1, deletions=0)],
        issue_key=None,
        jira_text=None,
        jira_warning="ISSUE_KEY_MISSING",
        previous_findings=[
            PreviousFinding(
                stable_id="blog-pt-index",
                severity="P1",
                title="Portuguese blog URLs still indexed",
                path="src/pages/Blog/Blog.jsx",
                line=28,
                status="open",
                evidence="loader не режет locale pt.",
                scenario="Открыл /pt/blog — страница живая.",
                recommendation="301 на /blog для не-en/ru.",
            )
        ],
        limits=limits,
    )
    assert "Portuguese blog URLs still indexed" in context.prompt
    assert "loader не режет locale pt." in context.prompt
    assert "301 на /blog" in context.prompt
