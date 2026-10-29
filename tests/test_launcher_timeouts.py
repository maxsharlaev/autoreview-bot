"""Timeout budget helpers and orphan TTL fail-closed checks."""

from __future__ import annotations

import pytest
from app.launcher.timeouts import (
    DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS,
    checkout_orphan_ttl_seconds,
    default_checkout_orphan_ttl_seconds,
    ensure_launcher_orphan_ttl_config,
    launcher_pipeline_overhead_seconds,
    max_review_timeout_seconds,
    min_checkout_orphan_ttl_seconds,
    validate_checkout_orphan_ttl,
)


def test_default_orphan_ttl_derived_from_max_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", raising=False)
    monkeypatch.delenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", raising=False)
    assert max_review_timeout_seconds() == DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS
    assert default_checkout_orphan_ttl_seconds() == (
        DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS + launcher_pipeline_overhead_seconds()
    )
    assert checkout_orphan_ttl_seconds() == default_checkout_orphan_ttl_seconds()


def test_orphan_ttl_follows_raised_max_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", raising=False)
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "800")
    assert checkout_orphan_ttl_seconds() == 800 + launcher_pipeline_overhead_seconds()


def test_validate_orphan_ttl_rejects_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "100")
    with pytest.raises(RuntimeError, match="OPR_CHECKOUT_ORPHAN_TTL_SECONDS"):
        validate_checkout_orphan_ttl(timeout_seconds=600)


def test_validate_orphan_ttl_accepts_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    needed = min_checkout_orphan_ttl_seconds(600)
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", str(needed))
    validate_checkout_orphan_ttl(timeout_seconds=600)


def test_ensure_launcher_orphan_ttl_config_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "100")
    with pytest.raises(RuntimeError, match="OPR_CHECKOUT_ORPHAN_TTL_SECONDS"):
        ensure_launcher_orphan_ttl_config()


def test_ensure_launcher_orphan_ttl_config_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", raising=False)
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    ensure_launcher_orphan_ttl_config()
