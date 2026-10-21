"""Progress status for manual (API-triggered) review runs."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import AppConfig
from app.owners.registry import RouteResult
from app.services import progress_status as ps
from tests.test_manual_review import _client
from tests.test_progress_status import COMMENT_URL, HEAD, FakeGitHub, Harness, _finding, _verified


def _manual(h: Harness, **summary) -> Harness:
    h.run.trigger = "manual"
    h.run.summary.update(summary)
    return h


@pytest.mark.parametrize("kind", ["app", "pat"])
@pytest.mark.asyncio
async def test_manual_run_reports_full_lifecycle(monkeypatch, tmp_path, kind) -> None:
    h = _manual(Harness(monkeypatch, tmp_path, kind=kind, verified=_verified([_finding("P1")])))
    await h.queue()
    result = await h.review()

    assert result.status == "completed"
    if kind == "app":
        assert h.github.check_states() == ["queued", "in_progress", "completed"]
        final = h.github.calls[-1][2]
        assert final["conclusion"] == "neutral" and final["details_url"] == COMMENT_URL
    else:
        statuses = h.github.payloads("status")
        assert [p["state"] for p in statuses] == ["pending", "pending", "success"]
        assert statuses[-1]["target_url"] == COMMENT_URL
    assert h.run.progress_status["state"] == "completed"


@pytest.mark.parametrize("kind", ["app", "pat"])
@pytest.mark.asyncio
async def test_manual_draft_is_skipped_with_reason(monkeypatch, tmp_path, kind) -> None:
    h = _manual(Harness(monkeypatch, tmp_path, kind=kind, draft=True))
    await h.queue()
    result = await h.review()
    assert result.status == "skipped"
    if kind == "app":
        final = h.github.calls[-1][2]
        assert final["conclusion"] == "skipped" and "draft" in final["output"]["summary"]
    else:
        last = h.github.payloads("status")[-1]
        assert last["state"] == "success" and "draft" in last["description"]


@pytest.mark.asyncio
async def test_manual_crash_is_failure(monkeypatch, tmp_path) -> None:
    h = _manual(Harness(monkeypatch, tmp_path, codex_error=RuntimeError("boom")))
    await h.queue()
    with pytest.raises(RuntimeError):
        await h.review()
    final = h.github.calls[-1][2]
    assert final["conclusion"] == "failure"
    assert "boom" not in str(final)


@pytest.mark.asyncio
async def test_manual_opt_out_reports_nothing(monkeypatch, tmp_path) -> None:
    h = _manual(Harness(monkeypatch, tmp_path), **{ps.PROGRESS_OPT_OUT: True})
    result = await h.review()
    assert result.status == "completed"
    assert h.github.calls == []


@pytest.mark.parametrize("trigger", ["digest", "schedule", "other"])
@pytest.mark.asyncio
async def test_other_triggers_report_nothing(monkeypatch, tmp_path, trigger) -> None:
    h = Harness(monkeypatch, tmp_path)
    h.run.trigger = trigger
    await h.review()
    assert h.github.calls == []


@pytest.mark.asyncio
async def test_manual_respects_owner_mode_off(monkeypatch, tmp_path) -> None:
    config = AppConfig(progress_status={"mode": "off"})
    h = _manual(Harness(monkeypatch, tmp_path, config=config))
    h.ctx.config = config
    await h.review()
    assert h.github.calls == []


@pytest.mark.asyncio
async def test_manual_disabled_owner_status_is_closed_as_skipped(monkeypatch, tmp_path) -> None:
    h = _manual(Harness(monkeypatch, tmp_path))
    await h.queue()
    h.registry.get.return_value = None
    h.registry.is_disabled.return_value = True
    h.registry.disabled_status_context.return_value = h.ctx
    result = await h.review()
    assert result.skip_reason == "owner_disabled"
    assert h.github.calls[-1][2]["conclusion"] == "skipped"


@pytest.mark.asyncio
async def test_superseded_worker_never_overwrites_a_closed_status(monkeypatch, tmp_path) -> None:
    # A newer run on the same commit already closed this run's status as cancelled while the
    # old worker was still reviewing: the old worker must leave it alone.
    h = _manual(Harness(monkeypatch, tmp_path))
    h.stored = {"mode": "check_run", "state": "completed", "conclusion": "cancelled", "check_run_id": 9}
    result = await h.review()
    assert result.status == "completed"
    assert h.github.calls == []


@pytest.mark.parametrize("report_status", [True, False])
def test_manual_endpoint_passes_owner_reporter_and_opt_out(report_status) -> None:
    ctx = SimpleNamespace(id="org-a", config=AppConfig(), github=SimpleNamespace(kind="app"))
    registry = MagicMock()
    registry.get.return_value = ctx
    registry.resolve.return_value = RouteResult("org-a", "exact")
    fake = FakeGitHub()
    run = SimpleNamespace(id=uuid.uuid4(), status="pending", head_sha=HEAD, trigger="manual")
    with (
        patch("app.api.v1.reviews.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake),
        patch("app.api.v1.reviews.queue_review", AsyncMock(return_value=run)) as queue,
    ):
        response = _client(with_api_key=True).post(
            "/api/v1/reviews",
            json={"repository": "org/repo", "number": 7, "head_sha": HEAD, "report_status": report_status},
        )
    assert response.status_code == 202
    kwargs = queue.await_args.kwargs
    assert kwargs["trigger"] == "manual"
    assert kwargs["progress"].mode == "check_run" and kwargs["progress"].github is fake
    assert kwargs["report_new_run"] is report_status
