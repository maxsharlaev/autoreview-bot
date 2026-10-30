"""Timeout budget helpers and orphan TTL fail-closed checks."""

from __future__ import annotations

import pytest
from app.launcher.timeouts import (
    DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS,
    LAUNCHER_CANCEL_DEADLINE_SECONDS,
    LAUNCHER_CANCEL_WAIT_SECONDS,
    REVIEW_CONTAINER_ORPHAN_GRACE_SECONDS,
    WORKER_CANCEL_HTTP_READ_SECONDS,
    ensure_launcher_orphan_ttl_config,
    max_review_timeout_seconds,
    min_review_container_orphan_ttl_seconds,
    review_orphan_ttl_seconds,
    validate_cancel_timeouts,
    validate_codex_timeout_against_arq,
    validate_review_container_orphan_ttl,
)


def test_cancel_wait_strictly_below_worker_delete_read() -> None:
    assert LAUNCHER_CANCEL_WAIT_SECONDS < LAUNCHER_CANCEL_DEADLINE_SECONDS
    assert LAUNCHER_CANCEL_DEADLINE_SECONDS < WORKER_CANCEL_HTTP_READ_SECONDS
    validate_cancel_timeouts()


def test_min_review_container_orphan_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", raising=False)
    assert max_review_timeout_seconds() == DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS
    assert min_review_container_orphan_ttl_seconds() == (
        DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS + REVIEW_CONTAINER_ORPHAN_GRACE_SECONDS
    )


def test_validate_review_orphan_ttl_rejects_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("OPR_REVIEW_ORPHAN_TTL_SECONDS", "60")
    with pytest.raises(RuntimeError, match="OPR_REVIEW_ORPHAN_TTL_SECONDS"):
        validate_review_container_orphan_ttl()


def test_validate_review_orphan_ttl_accepts_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    needed = min_review_container_orphan_ttl_seconds()
    monkeypatch.setenv("OPR_REVIEW_ORPHAN_TTL_SECONDS", str(needed))
    validate_review_container_orphan_ttl()
    assert review_orphan_ttl_seconds() == needed


def test_ensure_launcher_orphan_ttl_config_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("OPR_REVIEW_ORPHAN_TTL_SECONDS", "60")
    with pytest.raises(RuntimeError, match="OPR_REVIEW_ORPHAN_TTL_SECONDS"):
        ensure_launcher_orphan_ttl_config()


def test_ensure_launcher_orphan_ttl_config_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.delenv("OPR_REVIEW_ORPHAN_TTL_SECONDS", raising=False)
    ensure_launcher_orphan_ttl_config()


def test_codex_timeout_still_validated_against_arq() -> None:
    with pytest.raises(RuntimeError, match="worker_job_timeout"):
        validate_codex_timeout_against_arq(timeout_seconds=600, worker_job_timeout=650)
