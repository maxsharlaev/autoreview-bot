from app.config import LanguageYaml
from app.services.comment_render import FindingTransitionView, RenderInput, render_jira_comment, render_sticky_comment
from app.services.constants import REVIEW_MARKER
from app.services.publisher import empty_verified
from app.services.verifier import FindingView


def _render(**kwargs) -> RenderInput:
    verified = kwargs.pop("verified", None)
    if verified is None:
        verified = empty_verified(files_total=2, files_reviewed=2, skipped=[], truncated=False)
        verified.summary = "Looks safe."
    kwargs.setdefault("transitions", [])
    kwargs.setdefault("visibility", "private")
    return RenderInput(
        repository="org/repo",
        pr_number=7,
        head_sha="abc123",
        mode="advisory",
        run_id="run-1",
        issue_key="ABC-1",
        jira_warning=None,
        verified=verified,
        duration_ms=10,
        model="test-model",
        **kwargs,
    )


def test_sticky_comment_defaults_to_english() -> None:
    text = render_sticky_comment(_render())
    assert REVIEW_MARKER in text
    assert "**Verdict:**" in text
    assert "### Findings" in text
    assert "No blocking findings." in text


def test_sticky_comment_russian_labels() -> None:
    text = render_sticky_comment(_render(language=LanguageYaml(summary="ru", details="ru")))
    assert "### Замечания" in text
    assert "Критических замечаний нет." in text


def test_jira_comment_has_caught_checked_result() -> None:
    text = render_jira_comment(_render())
    assert "Caught:" in text
    assert "Checked:" in text
    assert "Result:" in text


def _open_finding() -> FindingView:
    return FindingView(
        stable_id="blog-pt-index",
        severity="P1",
        confidence=0.8,
        category="seo",
        path="src/pages/Blog/Blog.jsx",
        line=28,
        title="Portuguese blog URLs still indexed",
        scenario="Открыл /pt/blog — страница живая.",
        evidence="loader не режет locale pt.",
        recommendation="301 на /blog для не-en/ru.",
        blocking_candidate=True,
    )


def test_sticky_comment_keeps_full_open_finding() -> None:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.summary = "Previous SEO blocker is still open."
    verified.findings = [_open_finding()]
    text = render_sticky_comment(_render(verified=verified))
    assert "Portuguese blog URLs still indexed" in text
    assert "Открыл /pt/blog" in text
    assert "301 на /blog" in text
    assert "| ID | Статус |" not in text


def test_sticky_comment_transition_table() -> None:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.summary = "One still open, one resolved."
    verified.findings = [_open_finding()]
    text = render_sticky_comment(
        _render(
            verified=verified,
            transitions=[
                FindingTransitionView(
                    stable_id="blog-pt-index",
                    status="still_open",
                    path="src/pages/Blog/Blog.jsx",
                    line=28,
                    title="Portuguese blog URLs still indexed",
                ),
                FindingTransitionView(
                    stable_id="other-id",
                    status="resolved",
                    path="src/shared/api/blog.api.js",
                    line=23,
                    title="Locale list still includes pt",
                ),
            ],
        )
    )
    assert "### Previous finding transitions" in text
    assert "| Status | Finding | Path |" in text
    assert "`still_open`" in text
    assert "`resolved`" in text
    assert "Portuguese blog URLs still indexed" in text
    assert "Locale list still includes pt" in text
    assert "Открыл /pt/blog" in text


def test_sticky_comment_resolved_keeps_title() -> None:
    text = render_sticky_comment(
        _render(
            transitions=[
                FindingTransitionView(
                    stable_id="blog-pt-index",
                    status="resolved",
                    path="src/pages/Blog/Blog.jsx",
                    line=28,
                    title="Portuguese blog URLs still indexed",
                    evidence="redirect('/blog', 301) is in the loader.",
                )
            ]
        )
    )
    assert "### Previous finding transitions" in text
    assert "`resolved`" in text
    assert "Portuguese blog URLs still indexed" in text


def test_sticky_comment_shows_p3_count() -> None:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.summary = "Code improvement suggestions."
    verified.findings = [
        FindingView(
            stable_id="style-fix",
            severity="P3",
            confidence=0.7,
            category="style",
            path="src/utils.py",
            line=10,
            title="Consider simplifying expression",
            scenario="Expression could be cleaner.",
            evidence="Nested conditionals.",
            recommendation="Use early return pattern.",
            blocking_candidate=False,
        )
    ]
    text = render_sticky_comment(_render(verified=verified))
    assert "P0=0" in text
    assert "P1=0" in text
    assert "P2=0" in text
    assert "P3=1" in text
    assert "**Verdict:** clean" in text


def test_sticky_comment_verdict_clean_with_only_p3() -> None:
    """PR with only P3 findings should have verdict 'clean', not 'findings'."""
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.summary = "Minor suggestions only."
    verified.findings = [
        FindingView(
            stable_id="p3-only",
            severity="P3",
            confidence=0.6,
            category="style",
            path="src/main.py",
            line=20,
            title="Variable naming",
            scenario="Name could be clearer.",
            evidence="Single letter variable.",
            recommendation="Use descriptive name.",
            blocking_candidate=False,
        )
    ]
    text = render_sticky_comment(_render(verified=verified))
    assert "**Verdict:** clean" in text


def test_jira_comment_shows_p3_count() -> None:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.findings = [
        FindingView(
            stable_id="p3-jira",
            severity="P3",
            confidence=0.5,
            category="style",
            path="src/api.py",
            line=5,
            title="Style suggestion",
            scenario="Minor improvement.",
            evidence="Old pattern.",
            recommendation="New pattern.",
            blocking_candidate=False,
        )
    ]
    text = render_jira_comment(_render(verified=verified))
    assert "P0=0" in text
    assert "P1=0" in text
    assert "P2=0" in text
    assert "P3=1" in text


def test_carry_rehydrates_open_blocker() -> None:
    from types import SimpleNamespace

    from app.services.orchestrator import _carry_open_previous
    from app.services.verifier import PreviousFindingView

    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.previous_findings = [
        PreviousFindingView(
            stable_id="blog-pt-index",
            status="still_open",
            evidence="still indexed",
            path="src/pages/Blog/Blog.jsx",
            line=28,
        )
    ]
    stored = [
        SimpleNamespace(
            stable_id="blog-pt-index",
            severity="P1",
            confidence=None,
            category="seo",
            path="src/pages/Blog/Blog.jsx",
            line=28,
            title="Portuguese blog URLs still indexed",
            scenario="Открыл /pt/blog — страница живая.",
            evidence="loader не режет locale pt.",
            recommendation="301 на /blog для не-en/ru.",
            current_status="open",
        )
    ]
    _carry_open_previous(verified, stored)
    assert len(verified.findings) == 1
    assert verified.findings[0].title == "Portuguese blog URLs still indexed"
    assert "Открыл /pt/blog" in verified.findings[0].scenario
    assert "At this SHA: still indexed" in verified.findings[0].evidence
