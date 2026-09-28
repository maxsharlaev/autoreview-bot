from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.config import AppConfig, PrDescriptionYaml
from app.services import orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_review_is_complete_before_optional_description_failure(monkeypatch, failure: str) -> None:
    run = SimpleNamespace(status="running")
    events = []

    async def complete(*args):
        run.status = "completed"
        events.append("review-published")
        return run

    async def description_step():
        assert run.status == "completed"
        events.append("description-started")
        if failure == "exception":
            raise RuntimeError("description unavailable")
        await asyncio.sleep(0.05)

    monkeypatch.setattr(orchestrator, "_complete", complete)
    settings = PrDescriptionYaml.model_construct(enabled=True, timeout_seconds=0.001)
    session = SimpleNamespace(commit=AsyncMock())
    result = await orchestrator._complete_then_describe(
        session=session,
        run=run,
        started=0,
        verified=None,
        config=AppConfig(pr_description=settings),
        prompt_version="test",
        progress=SimpleNamespace(event=Mock()),
        description_step=description_step,
    )
    assert result is run and run.status == "completed"
    assert events == ["review-published", "description-started"]
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_description_is_not_started(monkeypatch) -> None:
    run = SimpleNamespace(status="running")

    async def complete(*args):
        run.status = "completed"
        return run

    monkeypatch.setattr(orchestrator, "_complete", complete)
    step = Mock()
    result = await orchestrator._complete_then_describe(
        session=SimpleNamespace(commit=AsyncMock()),
        run=run,
        started=0,
        verified=None,
        config=AppConfig(),
        prompt_version="test",
        progress=SimpleNamespace(event=Mock()),
        description_step=step,
    )
    assert result.status == "completed"
    step.assert_not_called()


@pytest.mark.asyncio
async def test_completed_run_is_not_republished_on_retry() -> None:
    run = SimpleNamespace(status="completed")
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: run)))
    github = SimpleNamespace(get_pull_request=AsyncMock())
    result = await orchestrator.run_review(
        session,
        uuid.uuid4(),
        config=AppConfig(),
        github=github,
    )
    assert result is run
    github.get_pull_request.assert_not_awaited()
