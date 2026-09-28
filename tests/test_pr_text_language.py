from __future__ import annotations

from app.config import AppConfig, LanguageYaml, PrTextYaml
from app.services.pr_description import build_pr_description_context, render_pr_description
from app.services.pr_text_language import output_language_matches, resolve_pr_language


def test_auto_prefers_human_title_then_body_then_commits() -> None:
    config = AppConfig(pr_text=PrTextYaml(language="auto"), language=LanguageYaml(details="en"))
    assert resolve_pr_language(config, human_title="Исправить проверку", human_body="English text", commits=[]) == "ru"
    assert resolve_pr_language(config, human_title="Fix validation", human_body="Русское описание", commits=[]) == "en"
    assert resolve_pr_language(config, human_title="Dev", human_body="Русское описание", commits=[]) == "ru"
    assert resolve_pr_language(config, human_title="ABC-1: Dev", human_body="Русское описание", commits=[]) == "ru"
    assert (
        resolve_pr_language(config, human_title="", human_body="", commits=["feat: Проверить входные данные"]) == "ru"
    )
    assert resolve_pr_language(config, human_title="", human_body="", commits=[]) == "en"


def test_any_language_code_is_accepted_with_fixed_fallback_headings() -> None:
    config = AppConfig(pr_text=PrTextYaml(language="fr"))
    assert resolve_pr_language(config, human_title="", human_body="", commits=[]) == "fr"
    prompt = build_pr_description_context(
        title="",
        body="",
        head_ref="feature/a",
        base_sha="b" * 40,
        head_sha="a" * 40,
        files=[],
        commit_messages=[],
        jira_issue=None,
        language="fr",
    )
    assert "language code fr" in prompt
    draft = render_pr_description(
        {"summary": "Résumé", "changes": "Changements", "linked_task": "", "testing": "", "notes_risks": ""},
        language="fr",
        linked_task=False,
    )
    assert draft.startswith("## Summary\n\nRésumé")


def test_language_check_ignores_english_conventional_prefix() -> None:
    russian = {
        "summary": "Проверка входных данных",
        "changes": "Добавлена проверка формы",
        "testing": "Тесты не запускались",
        "notes_risks": "Риски не выявлены",
        "suggested_title": "feat(form): Проверить входные данные",
        "output_language": "ru",
    }
    assert output_language_matches(russian, "ru")
    assert not output_language_matches(russian | {"summary": "Validate inputs", "changes": "Add form checks"}, "ru")
    assert not output_language_matches(russian | {"output_language": "en"}, "ru")
