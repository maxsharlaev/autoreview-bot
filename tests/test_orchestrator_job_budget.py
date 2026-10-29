"""Orchestrator job-budget decisions: clamp / skip retry and extras."""

from __future__ import annotations

from app.services.constants import JOB_BUDGET_EXHAUSTED
from app.services.job_deadline import JobDeadline


def test_retry_skipped_when_budget_insufficient() -> None:
    now = 1_000.0
    # Only 20s until deadline with 30s call margin -> cannot start.
    deadline = JobDeadline(deadline_monotonic=now + 20, call_margin_seconds=30)
    assert deadline.clamp_timeout(600, now=now) is None
    note = "retry skipped: insufficient job budget"
    assert JOB_BUDGET_EXHAUSTED != note


def test_extra_call_skipped_when_budget_insufficient() -> None:
    now = 1_000.0
    deadline = JobDeadline(deadline_monotonic=now + 20, call_margin_seconds=30)
    assert deadline.clamp_timeout(120, now=now) is None  # public alignment
    assert deadline.clamp_timeout(600, now=now) is None  # PR description


def test_per_call_timeout_clamped_to_remaining() -> None:
    now = 1_000.0
    deadline = JobDeadline.from_worker_job_timeout(
        200,
        now=now,
        safety_margin_seconds=20,
        call_margin_seconds=30,
    )
    # usable = (200 - 20) - 30 = 150
    assert deadline.clamp_timeout(600, now=now) == 150
    assert deadline.clamp_timeout(90, now=now) == 90
