"""ARB-27-012: post-publication budget notices refresh the sticky comment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.config import LanguageYaml
from app.services.comment_render import RenderInput
from app.services.orchestrator import _append_post_publish_budget_note, empty_verified
from app.services.publisher import Publisher


@pytest.mark.asyncio
async def test_post_publish_budget_note_refreshes_sticky() -> None:
    """First budget check can pass; a later failure updates the sticky with the notice."""
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
        notes=[],
        visibility="public",
    )
    upserts: list[str] = []

    class FakeGitHub:
        async def upsert_sticky_comment(self, owner, repo, pr_number, body):  # noqa: ANN001
            upserts.append(body)
            return "https://example.com/comment/1"

    publisher = Publisher(github=FakeGitHub(), config=SimpleNamespace())  # type: ignore[arg-type]

    async def sticky_refresh(notes: list[str]) -> None:
        render.notes = list(notes)
        await publisher.update_sticky(owner="o", repo="r", pr_number=1, render=render)

    notes: list[str] = []
    # Simulate: pre-publish check passed (no note yet). Later check fails.
    await _append_post_publish_budget_note(
        notes,
        "PR description skipped: insufficient job budget",
        sticky_refresh=sticky_refresh,
    )
    assert notes == ["PR description skipped: insufficient job budget"]
    assert len(upserts) == 1
    assert "PR description skipped: insufficient job budget" in upserts[0]
    # Idempotent refresh with same note does not duplicate the note text meaninglessly
    await _append_post_publish_budget_note(
        notes,
        "PR description skipped: insufficient job budget",
        sticky_refresh=sticky_refresh,
    )
    assert notes == ["PR description skipped: insufficient job budget"]
    assert len(upserts) == 2
