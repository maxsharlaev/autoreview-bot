"""Container-runner env gate for worker startup."""

from __future__ import annotations

import pytest
from app.services.container_runner_env import ensure_container_runner_env


def test_in_process_allows_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAUNCHER_TOKEN", raising=False)
    ensure_container_runner_env(runner_backend="in_process")


def test_container_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAUNCHER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="LAUNCHER_TOKEN"):
        ensure_container_runner_env(runner_backend="container")


def test_container_rejects_short_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "too-short")
    with pytest.raises(RuntimeError, match="LAUNCHER_TOKEN"):
        ensure_container_runner_env(runner_backend="container")


def test_container_accepts_long_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    ensure_container_runner_env(runner_backend="container")


def test_container_rejects_timeout_vs_arq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    with pytest.raises(RuntimeError, match="worker_job_timeout"):
        ensure_container_runner_env(
            runner_backend="container",
            timeout_seconds=850,
            worker_job_timeout=900,
        )


def test_container_accepts_timeout_under_arq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    ensure_container_runner_env(
        runner_backend="container",
        timeout_seconds=600,
        worker_job_timeout=900,
    )


def test_container_rejects_orphan_ttl_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "100")
    with pytest.raises(RuntimeError, match="OPR_CHECKOUT_ORPHAN_TTL_SECONDS"):
        ensure_container_runner_env(
            runner_backend="container",
            timeout_seconds=600,
            worker_job_timeout=900,
        )


def test_container_accepts_orphan_ttl_covering_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    monkeypatch.delenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", raising=False)
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    ensure_container_runner_env(
        runner_backend="container",
        timeout_seconds=600,
        worker_job_timeout=900,
    )
