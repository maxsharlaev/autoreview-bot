"""Admin finding suppression (accepted / false_positive) — orchestrator behavior."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.config import LanguageYaml
from app.models import Finding
from app.services.comment_render import RenderInput, render_sticky_comment
from app.services.finding_status import (
    FINDING_STATUS_ACCEPTED,
    FINDING_STATUS_FALSE_POSITIVE,
    FINDING_STATUS_OPEN,
    clear_finding_suppression,
    findings_for_model_previous,
    is_suppressed_status,
    set_finding_suppression,
    validate_suppression_status,
)
from app.services.orchestrator import _carry_open_previous, _store_findings
from app.services.publisher import empty_verified
from app.services.verifier import FindingView


def _view(stable_id: str = "sup-1", *, title: str = "Issue") -> FindingView:
    return FindingView(
        stable_id=stable_id,
        severity="P2",
        confidence=0.9,
        category="correctness",
        path="app/x.py",
        line=10,
        title=title,
        scenario="scenario",
        evidence="evidence",
        recommendation="fix it",
        blocking_candidate=False,
    )


def _stored(
    stable_id: str = "sup-1",
    *,
    status: str = FINDING_STATUS_ACCEPTED,
) -> Finding:
    return Finding(
        id=uuid.uuid4(),
        pull_request_id=uuid.uuid4(),
        first_seen_run_id=uuid.uuid4(),
        last_seen_run_id=uuid.uuid4(),
        owner_id="default",
        stable_id=stable_id,
        severity="P2",
        category="correctness",
        path="app/x.py",
        line=10,
        title="Issue",
        scenario="scenario",
        evidence="evidence",
        recommendation="fix it",
        current_status=status,
    )


def test_validate_suppression_status() -> None:
    assert validate_suppression_status("accepted") == FINDING_STATUS_ACCEPTED
    assert validate_suppression_status("FALSE_POSITIVE") == FINDING_STATUS_FALSE_POSITIVE
    with pytest.raises(ValueError):
        validate_suppression_status("open")
    with pytest.raises(ValueError):
        validate_suppression_status("resolved")


def test_suppressed_not_carried_as_open() -> None:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.findings = []
    suppressed = _stored(status=FINDING_STATUS_ACCEPTED)
    open_other = _stored("other", status=FINDING_STATUS_OPEN)
    _carry_open_previous(verified, [suppressed, open_other], visibility="private")
    ids = {item.stable_id for item in verified.findings}
    assert "other" in ids
    assert "sup-1" not in ids


@pytest.mark.asyncio
async def test_suppressed_not_reopened_when_model_re_reports() -> None:
    prior = _stored(status=FINDING_STATUS_FALSE_POSITIVE)
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    view = _view()
    verified.findings = [view]
    session = SimpleNamespace(add=lambda _: None, flush=AsyncMock())
    pr = SimpleNamespace(findings=[prior])
    run = SimpleNamespace(id=uuid.uuid4())
    transitions = await _store_findings(
        session,
        pr,
        run,
        verified,
        fresh_ids={view.stable_id},
        details_visibility="private",
        owner_id="default",
        owner_findings=[prior],
    )
    assert prior.current_status == FINDING_STATUS_FALSE_POSITIVE
    assert prior.last_seen_run_id == run.id
    assert view.stable_id not in {item.stable_id for item in verified.findings}
    assert all(item.stable_id != view.stable_id for item in transitions)
    assert not any(item.status == "regressed" for item in transitions)


@pytest.mark.asyncio
async def test_resolved_still_regresses_when_model_re_reports() -> None:
    prior = _stored(status="resolved")
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    view = _view()
    verified.findings = [view]
    session = SimpleNamespace(add=lambda _: None, flush=AsyncMock())
    pr = SimpleNamespace(findings=[prior])
    run = SimpleNamespace(id=uuid.uuid4())
    transitions = await _store_findings(
        session,
        pr,
        run,
        verified,
        fresh_ids={view.stable_id},
        details_visibility="private",
        owner_id="default",
        owner_findings=[prior],
    )
    assert prior.current_status == FINDING_STATUS_OPEN
    assert any(item.status == "regressed" for item in transitions)
    assert view.stable_id in {item.stable_id for item in verified.findings}


@pytest.mark.asyncio
async def test_suppressed_not_shown_in_sticky_comment() -> None:
    prior = _stored(status=FINDING_STATUS_ACCEPTED)
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    view = _view(title="ShouldNotAppear")
    verified.findings = [view]
    verified.summary = "ok"
    session = SimpleNamespace(add=lambda _: None, flush=AsyncMock())
    pr = SimpleNamespace(findings=[prior])
    run = SimpleNamespace(id=uuid.uuid4())
    transitions = await _store_findings(
        session,
        pr,
        run,
        verified,
        fresh_ids={view.stable_id},
        details_visibility="private",
        owner_id="default",
        owner_findings=[prior],
    )
    render = RenderInput(
        repository="o/r",
        pr_number=1,
        head_sha="a" * 40,
        mode="advisory",
        run_id=str(run.id),
        issue_key=None,
        jira_warning=None,
        verified=verified,
        transitions=transitions,
        duration_ms=1,
        analysed_at=None,
        model="m",
        language=LanguageYaml(),
    )
    body = render_sticky_comment(render)
    assert "ShouldNotAppear" not in body
    assert "sup-1" not in body
    assert "regressed" not in body


@pytest.mark.asyncio
async def test_set_and_clear_suppression_helpers() -> None:
    finding = _stored(status=FINDING_STATUS_OPEN)
    finding.pull_request_id = uuid.uuid4()

    class FakeResult:
        def __init__(self, row: Finding | None) -> None:
            self._row = row

        def scalar_one_or_none(self) -> Finding | None:
            return self._row

    class FakeSession:
        def __init__(self, row: Finding) -> None:
            self.row = row
            self.flushed = False

        async def execute(self, _stmt):  # noqa: ANN001
            return FakeResult(self.row)

        async def flush(self) -> None:
            self.flushed = True

    session = FakeSession(finding)
    updated = await set_finding_suppression(
        session,
        owner_id="default",
        pull_request_id=finding.pull_request_id,
        stable_id=finding.stable_id,
        status="accepted",
    )
    assert updated.current_status == FINDING_STATUS_ACCEPTED
    assert is_suppressed_status(updated.current_status)
    cleared = await clear_finding_suppression(
        session,
        owner_id="default",
        pull_request_id=finding.pull_request_id,
        stable_id=finding.stable_id,
    )
    assert cleared.current_status == FINDING_STATUS_OPEN
    assert session.flushed


def test_suppressed_excluded_from_model_previous() -> None:
    suppressed = _stored(status=FINDING_STATUS_ACCEPTED)
    open_one = _stored("keep", status=FINDING_STATUS_OPEN)
    out = findings_for_model_previous([suppressed, open_one])
    assert [f.stable_id for f in out] == ["keep"]
