"""Job wall-clock deadline for multi-call ARQ review jobs."""

from __future__ import annotations

from app.services.job_deadline import (
    CODEX_CALL_BUDGET_MARGIN_SECONDS,
    JOB_DEADLINE_SAFETY_MARGIN_SECONDS,
    MIN_CODEX_CALL_SECONDS,
    JobDeadline,
)


def test_clamp_timeout_uses_min_of_configured_and_remaining() -> None:
    now = 1_000.0
    deadline = JobDeadline.from_worker_job_timeout(
        200,
        now=now,
        safety_margin_seconds=20,
        call_margin_seconds=30,
    )
    # usable = 200-20-30 = 150
    assert deadline.clamp_timeout(600, now=now) == 150
    assert deadline.clamp_timeout(100, now=now) == 100


def test_clamp_timeout_none_when_budget_insufficient() -> None:
    now = 1_000.0
    deadline = JobDeadline(
        deadline_monotonic=now + 10,
        call_margin_seconds=30,
    )
    assert deadline.clamp_timeout(60, now=now) is None


def test_from_worker_job_timeout_applies_safety_margin() -> None:
    now = 1000.0
    deadline = JobDeadline.from_worker_job_timeout(900, now=now, safety_margin_seconds=30)
    assert deadline.deadline_monotonic == 1000.0 + 870
    assert JOB_DEADLINE_SAFETY_MARGIN_SECONDS == 30
    assert CODEX_CALL_BUDGET_MARGIN_SECONDS == 30
    assert MIN_CODEX_CALL_SECONDS == 5
