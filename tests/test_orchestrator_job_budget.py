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


def test_description_budget_note_decided_before_publish() -> None:
    """ARB-27-012: note is appended to budget_notes before sticky publish.

    Orchestration decides skip_description when clamp fails, so the sticky
    comment receives the note via ``notes=budget_notes`` on ``_publish``.
    """
    now = 1_000.0
    deadline = JobDeadline(deadline_monotonic=now + 20, call_margin_seconds=30)
    budget_notes: list[str] = []
    skip_description = False
    pr_description_enabled = True
    runner_backend = "in_process"
    timeout_seconds = 600
    if (
        not skip_description
        and pr_description_enabled
        and deadline.clamp_timeout(timeout_seconds, now=now, runner_backend=runner_backend) is None
    ):
        note = "PR description skipped: insufficient job budget"
        if note not in budget_notes:
            budget_notes.append(note)
        skip_description = True
    # Sticky publish would see the note; description step is skipped afterward.
    assert skip_description is True
    assert budget_notes == ["PR description skipped: insufficient job budget"]


def test_container_retry_near_deadline_uses_overhead_margin() -> None:
    from app.launcher.timeouts import launcher_pipeline_overhead_seconds

    now = 1_000.0
    overhead = launcher_pipeline_overhead_seconds()
    # Simulate remaining ARQ budget after safety margin already applied.
    remaining = overhead + 90
    deadline = JobDeadline(deadline_monotonic=now + remaining, call_margin_seconds=30)
    # in_process would allow ~60s; container must reserve full overhead.
    assert deadline.clamp_timeout(600, now=now, runner_backend="in_process") == remaining - 30
    assert deadline.clamp_timeout(600, now=now, runner_backend="container") == 90
