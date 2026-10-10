from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.config import PublicReposYaml
from app.models import Finding
from app.services.comment_render import render_sticky_comment
from app.services.orchestrator import _carry_open_previous, _store_findings
from app.services.publisher import empty_verified
from tests.test_public_disclosure import SECRET, _render, _security


def _finding(*, visibility: str | None) -> Finding:
    return Finding(
        id=uuid.uuid4(),
        stable_id="secret-finding",
        severity="P2",
        category="Security",
        path="src/auth.py",
        line=42,
        title=SECRET,
        scenario=SECRET,
        evidence=SECRET,
        recommendation=SECRET,
        details_visibility=visibility,
        current_status="open",
    )


def test_public_repeat_preserves_public_context_and_hides_private_context() -> None:
    for provenance, expected in [("public", SECRET), ("private", ""), (None, "")]:
        fresh = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
        fresh.findings = [_security()]
        fresh.findings[0].scenario = ""
        _carry_open_previous(fresh, [_finding(visibility=provenance)], visibility="public")
        assert fresh.findings[0].scenario == expected


def test_public_repeat_can_render_fresh_details_under_explicit_full_policy() -> None:
    render = _render(PublicReposYaml(security_findings="full"))
    render.verified.summary = "Safe summary"
    assert SECRET in render_sticky_comment(render)
    render.redacted_prior_ids = {"secret-finding"}
    assert SECRET not in render_sticky_comment(render)


@pytest.mark.asyncio
async def test_incomplete_public_repeat_keeps_private_database_details() -> None:
    prior = _finding(visibility="private")
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    view = _security()
    view.scenario = ""
    verified.findings = [view]
    session = SimpleNamespace(add=lambda _: None, flush=AsyncMock())
    pr = SimpleNamespace(findings=[prior])
    run = SimpleNamespace(id=uuid.uuid4())
    await _store_findings(
        session, pr, run, verified, fresh_ids={view.stable_id}, details_visibility="public", owner_id="default"
    )
    assert prior.scenario == SECRET
    assert prior.details_visibility == "private"
