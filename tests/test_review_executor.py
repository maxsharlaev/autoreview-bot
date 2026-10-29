"""M1: ReviewExecutor factory and runner_backend config."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import CodexYaml
from app.services.review_executor import (
    CONTAINER_BACKEND,
    IN_PROCESS_BACKEND,
    InProcessReviewExecutor,
    get_review_executor,
)
from pydantic import ValidationError


def test_get_review_executor_in_process() -> None:
    executor = get_review_executor(IN_PROCESS_BACKEND)
    assert isinstance(executor, InProcessReviewExecutor)


def test_get_review_executor_container() -> None:
    from app.services.container_executor import ContainerReviewExecutor

    assert isinstance(get_review_executor(CONTAINER_BACKEND), ContainerReviewExecutor)


def test_get_review_executor_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown codex.runner_backend"):
        get_review_executor("firecracker")


def test_codex_yaml_default_runner_backend() -> None:
    cfg = CodexYaml()
    assert cfg.runner_backend == IN_PROCESS_BACKEND


def test_codex_yaml_accepts_container() -> None:
    assert CodexYaml(runner_backend="container").runner_backend == CONTAINER_BACKEND


def test_codex_yaml_rejects_unknown_backend() -> None:
    with pytest.raises(ValidationError, match="unknown codex.runner_backend"):
        CodexYaml(runner_backend="wasm")


@pytest.mark.asyncio
async def test_in_process_executor_delegates_to_run_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """InProcessReviewExecutor must call run_codex with the same kwargs (no behavior change)."""
    seen: dict = {}

    async def fake_run_codex(**kwargs):
        seen.update(kwargs)
        return '{"ok": true}'

    monkeypatch.setattr("app.services.review_executor.run_codex", fake_run_codex)
    executor = InProcessReviewExecutor()
    checkout = tmp_path / "co"
    checkout.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    result = await executor.run(
        checkout=checkout,
        prompt="hello",
        schema_path=schema,
        output_path=out,
        model="gpt-test",
        timeout_seconds=30,
        openai_api_key="sk-test",
        reasoning_effort="low",
        sandbox="read-only",
        approval_policy="never",
        allow_unsandboxed=False,
    )
    assert result == '{"ok": true}'
    assert seen["checkout"] == checkout
    assert seen["prompt"] == "hello"
    assert seen["schema_path"] == schema
    assert seen["output_path"] == out
    assert seen["model"] == "gpt-test"
    assert seen["timeout_seconds"] == 30
    assert seen["openai_api_key"] == "sk-test"
    assert seen["reasoning_effort"] == "low"
    assert seen["sandbox"] == "read-only"
    assert seen["approval_policy"] == "never"
    assert seen["allow_unsandboxed"] is False


@pytest.mark.asyncio
async def test_in_process_neutralize_runs_via_to_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB-27-023: neutralize_escaping_symlinks must not block the event loop."""
    import threading

    import app.services.review_executor as re

    main_ident = threading.get_ident()
    called_off_loop: list[bool] = []

    def fake_neutralize(checkout: Path) -> int:
        called_off_loop.append(threading.get_ident() != main_ident)
        return 0

    async def fake_run_codex(**kwargs):
        return "{}"

    monkeypatch.setattr(re, "neutralize_escaping_symlinks", fake_neutralize)
    monkeypatch.setattr(re, "run_codex", fake_run_codex)

    checkout = tmp_path / "co"
    checkout.mkdir()
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")
    await InProcessReviewExecutor().run(
        checkout=checkout,
        prompt="p",
        schema_path=schema,
        output_path=tmp_path / "o.json",
        model="m",
        timeout_seconds=10,
        openai_api_key="sk",
    )
    assert called_off_loop == [True]
