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
    assert "| Status | Severity | Finding | Path |" in text
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


def _severity_transitions() -> list[FindingTransitionView]:
    return [
        FindingTransitionView(stable_id="a", status="still_open", path="a.py", line=1, severity="P0", title="A"),
        FindingTransitionView(stable_id="b", status="regressed", path="b.py", line=2, severity="P1", title="B"),
        FindingTransitionView(stable_id="c", status="needs_human", path="c.py", line=3, severity="P2", title="C"),
        FindingTransitionView(stable_id="d", status="still_open", path="d.py", line=4, severity="P3", title="D"),
        FindingTransitionView(stable_id="e", status="resolved", path="e.py", line=5, severity="P1", title="E"),
        FindingTransitionView(stable_id="f", status="obsolete", path="f.py", line=6, severity="P2", title="F"),
    ]


def test_transition_table_shows_severity_with_light_only_for_open_findings() -> None:
    text = render_sticky_comment(_render(transitions=_severity_transitions()))
    assert "| `still_open` | 🔴 P0 | A | `a.py:1` |" in text
    assert "| `regressed` | 🟠 P1 | B | `b.py:2` |" in text
    assert "| `needs_human` | 🟡 P2 | C | `c.py:3` |" in text
    assert "| `still_open` | 🟢 P3 | D | `d.py:4` |" in text
    assert "| `resolved` | P1 | E | `e.py:5` |" in text
    assert "| `obsolete` | P2 | F | `f.py:6` |" in text


def test_transition_table_russian_header_same_cells() -> None:
    text = render_sticky_comment(
        _render(transitions=_severity_transitions(), language=LanguageYaml(summary="ru", details="ru"))
    )
    assert "| Статус | Важность | Замечание | Путь |" in text
    assert "| `regressed` | 🟠 P1 | B | `b.py:2` |" in text
    assert "| `resolved` | P1 | E | `e.py:5` |" in text


def test_transition_table_missing_or_unknown_severity() -> None:
    text = render_sticky_comment(
        _render(
            transitions=[
                FindingTransitionView(stable_id="x", status="still_open", path="x.py", title="X"),
                FindingTransitionView(stable_id="y", status="still_open", path="y.py", severity="P9", title="Y"),
            ]
        )
    )
    assert "| `still_open` | — | X | `x.py` |" in text
    assert "| `still_open` | P9 | Y | `y.py` |" in text


def test_redacted_transition_rows_keep_severity_and_light() -> None:
    from app.config import PublicReposYaml

    transitions = [
        FindingTransitionView(
            stable_id="sec",
            status="still_open",
            path="app/auth.py",
            line=12,
            severity="P1",
            title="Token leak in logs",
            category="security",
            evidence="logs the token",
        ),
        FindingTransitionView(
            stable_id="sec-fixed",
            status="resolved",
            path="app/auth.py",
            line=30,
            severity="P0",
            title="SQL injection",
            category="security",
        ),
        FindingTransitionView(
            stable_id="prior", status="still_open", path="app/old.py", line=3, severity="P2", title="Old private"
        ),
    ]
    for policy in ("redact", "redact_all"):
        text = render_sticky_comment(
            _render(
                transitions=transitions,
                visibility="public",
                public_repos=PublicReposYaml(security_findings=policy),
                redacted_prior_ids={"prior"},
            )
        )
        assert "| `still_open` | 🟠 P1 | Potential security issue. | `app/auth.py:12` |" in text
        assert "| `resolved` | P0 | Potential security issue. | `app/auth.py:30` |" in text
        assert "| `still_open` | 🟡 P2 | Previously reported issue. | `app/old.py:3` |" in text
        assert "Token leak" not in text
        assert "SQL injection" not in text
        assert "Old private" not in text


def _leftover(stable_id: str, status: str, severity: str, title: str, **kwargs) -> FindingTransitionView:
    return FindingTransitionView(
        stable_id=stable_id, status=status, path=f"{stable_id}.py", line=1, severity=severity, title=title, **kwargs
    )


def test_still_open_card_headings_show_light_and_severity() -> None:
    text = render_sticky_comment(
        _render(
            transitions=[
                _leftover("a", "still_open", "P0", "A"),
                _leftover("b", "regressed", "P1", "B"),
                _leftover("c", "needs_human", "P2", "C"),
                _leftover("d", "still_open", "P3", "D"),
                _leftover("e", "still_open", "", "E"),
            ]
        )
    )
    assert "#### [🔴 P0] [still_open] A" in text
    assert "#### [🟠 P1] [regressed] B" in text
    assert "#### [🟡 P2] [needs_human] C" in text
    assert "#### [🟢 P3] [still_open] D" in text
    assert "#### [still_open] E" in text


def test_still_open_card_heading_russian_same_marker() -> None:
    text = render_sticky_comment(
        _render(transitions=[_leftover("b", "regressed", "P1", "B")], language=LanguageYaml(summary="ru", details="ru"))
    )
    assert "### Ещё открыто с прошлого ревью" in text
    assert "#### [🟠 P1] [regressed] B" in text


def test_redacted_still_open_cards_keep_light_and_severity() -> None:
    from app.config import PublicReposYaml

    transitions = [
        _leftover("sec", "still_open", "P1", "Token leak in logs", category="security"),
        _leftover("prior", "regressed", "P0", "Old private"),
    ]
    for policy in ("redact", "redact_all"):
        text = render_sticky_comment(
            _render(
                transitions=transitions,
                visibility="public",
                public_repos=PublicReposYaml(security_findings=policy),
                redacted_prior_ids={"prior"},
            )
        )
        assert "#### [🟠 P1] Potential security issue." in text
        assert "#### [🔴 P0] Previously reported issue." in text
        assert "Token leak" not in text
        assert "Old private" not in text
