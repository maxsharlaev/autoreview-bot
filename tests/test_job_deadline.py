"""Job wall-clock deadline for multi-call ARQ review jobs."""

from __future__ import annotations

from app.launcher.timeouts import launcher_pipeline_overhead_seconds
from app.services.job_deadline import (
    CODEX_CALL_BUDGET_MARGIN_SECONDS,
    JOB_DEADLINE_SAFETY_MARGIN_SECONDS,
    MIN_CODEX_CALL_SECONDS,
    JobDeadline,
    call_margin_seconds_for_backend,
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


def test_container_backend_uses_full_launcher_overhead_margin() -> None:
    overhead = launcher_pipeline_overhead_seconds()
    assert call_margin_seconds_for_backend("container") == overhead
    assert call_margin_seconds_for_backend("in_process") == CODEX_CALL_BUDGET_MARGIN_SECONDS
    assert call_margin_seconds_for_backend(None) == CODEX_CALL_BUDGET_MARGIN_SECONDS


def test_clamp_timeout_container_near_arq_deadline() -> None:
    """Container retry near ARQ deadline: timeout + overhead must fit remaining."""
    now = 1_000.0
    overhead = launcher_pipeline_overhead_seconds()
    # Remaining to deadline = 200s; after overhead margin usable = 200 - overhead.
    deadline = JobDeadline(deadline_monotonic=now + 200, call_margin_seconds=30)
    clamped = deadline.clamp_timeout(600, now=now, runner_backend="container")
    assert clamped == 200 - overhead
    # HTTP read budget for that call would be clamped + overhead == remaining.
    assert clamped + overhead == 200
    # With only overhead + 4s left, container cannot start (usable=4 < MIN).
    tight = JobDeadline(deadline_monotonic=now + overhead + 4, call_margin_seconds=30)
    assert tight.clamp_timeout(600, now=now, runner_backend="container") is None
    # Same remaining with in_process margin (30) still allows a long call.
    assert tight.clamp_timeout(600, now=now, runner_backend="in_process") == overhead + 4 - 30
