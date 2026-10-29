"""Budget notes appear in sticky comments like quota/error codes."""

from __future__ import annotations

from app.config import LanguageYaml
from app.services.comment_render import RenderInput, render_sticky_comment
from app.services.orchestrator import empty_verified


def test_sticky_comment_includes_budget_notes() -> None:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.summary = "ok"
    render = RenderInput(
        repository="o/r",
        pr_number=1,
        head_sha="abc",
        mode="advisory",
        run_id="run-1",
        issue_key=None,
        jira_warning=None,
        verified=verified,
        transitions=[],
        duration_ms=10,
        model="gpt-test",
        language=LanguageYaml(),
        notes=["retry skipped: insufficient job budget", "PR description skipped: insufficient job budget"],
    )
    text = render_sticky_comment(render)
    assert "retry skipped: insufficient job budget" in text
    assert "PR description skipped: insufficient job budget" in text
