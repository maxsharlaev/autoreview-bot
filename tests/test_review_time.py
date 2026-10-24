from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app.config import AppConfig, LanguageYaml
from app.services import orchestrator
from app.services import progress_status as ps
from app.services.comment_render import RenderInput, render_sticky_comment
from app.services.publisher import empty_verified
from app.services.review_time import (
    analysis_record,
    format_duration,
    format_utc,
    reviewed_text,
    status_line,
)

SHA = "abc1234def5678"
FINISHED = datetime(2026, 10, 24, 9, 25, 40, tzinfo=UTC)


def _render(**kwargs) -> RenderInput:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.summary = "Looks fine."
    kwargs.setdefault("visibility", "private")
    return RenderInput(
        repository="org/repo",
        pr_number=7,
        head_sha=SHA,
        mode="advisory",
        run_id="run-1",
        issue_key=None,
        jira_warning=None,
        verified=verified,
        transitions=[],
        duration_ms=192_000,
        model="test-model",
        **kwargs,
    )


def test_format_utc_converts_other_zones_and_naive_values() -> None:
    moscow = timezone(timedelta(hours=3))
    assert format_utc(datetime(2026, 10, 24, 12, 25, 40, tzinfo=moscow)) == "2026-10-24 09:25:40 UTC"
    assert format_utc(datetime(2026, 10, 24, 9, 25, 40)) == "2026-10-24 09:25:40 UTC"


@pytest.mark.parametrize(
    ("ms", "en", "ru"),
    [
        (0, "0s", "0 с"),
        (45_400, "45s", "45 с"),
        (192_000, "3m 12s", "3 мин 12 с"),
        (3_723_000, "1h 02m 03s", "1 ч 02 мин 03 с"),
    ],
)
def test_format_duration(ms: int, en: str, ru: str) -> None:
    assert format_duration(ms) == en
    assert format_duration(ms, "ru") == ru


def test_comment_shows_short_sha_utc_time_and_duration() -> None:
    text = render_sticky_comment(_render(analysed_at=FINISHED))
    assert "**Reviewed:** commit `abc1234` at 2026-10-24 09:25:40 UTC (took 3m 12s)" in text
    assert f"**SHA:** `{SHA}`" in text


def test_comment_in_russian() -> None:
    text = render_sticky_comment(_render(analysed_at=FINISHED, language=LanguageYaml(summary="ru", details="ru")))
    assert "**Проверено:** коммит `abc1234` в 2026-10-24 09:25:40 UTC (заняло 3 мин 12 с)" in text


def test_comment_without_timestamp_has_no_line() -> None:
    assert "Reviewed:" not in render_sticky_comment(_render())


def test_public_redaction_keeps_the_timestamp() -> None:
    text = render_sticky_comment(_render(analysed_at=FINISHED, visibility="public"))
    assert "at 2026-10-24 09:25:40 UTC" in text


def test_status_summary_uses_the_stored_record() -> None:
    summary = {"finding_counts": {}, "analysis": analysis_record(SHA, FINISHED, 192_000)}
    assert status_line(summary) == "Reviewed commit abc1234 at 2026-10-24 09:25:40 UTC (took 3m 12s)."
    outcome = ps.final_outcome(
        status="completed",
        error_code=None,
        skip_reason=None,
        summary=summary,
        comment_url=None,
        issue_key=None,
        visibility="public",
        config=AppConfig(),
    )
    assert outcome.summary.endswith("Reviewed commit abc1234 at 2026-10-24 09:25:40 UTC (took 3m 12s).")


@pytest.mark.parametrize("summary", [None, {}, {"analysis": {}}, {"analysis": {"finished_at": "bad", "head_sha": "x"}}])
def test_status_line_without_record(summary) -> None:
    assert status_line(summary) is None


def test_reviewed_text_without_duration() -> None:
    assert reviewed_text(SHA, FINISHED, None) == "commit `abc1234` at 2026-10-24 09:25:40 UTC"


async def test_complete_keeps_the_analysis_record() -> None:
    record = analysis_record(SHA, FINISHED, 192_000)
    run = SimpleNamespace(summary={"analysis": record}, trigger="manual", owner_id="default")

    class Session:
        async def commit(self) -> None:
            return None

    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    await orchestrator._complete(Session(), run, 0.0, None, verified, AppConfig(), "v1")
    assert run.summary["analysis"] == record
    assert run.status == "completed"
