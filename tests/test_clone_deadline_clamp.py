"""ARB-27-018: clone_head clamps to JobDeadline."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from app.services.git_clone import (
    CLONE_CLEANUP_OVERHEAD_SECONDS,
    MIN_CLONE_SECONDS,
    CloneBudgetExhausted,
    clone_timeout_for_deadline,
)
from app.services.job_deadline import (
    MIN_CODEX_CALL_SECONDS,
    JobDeadline,
    call_margin_seconds_for_backend,
)


def test_clone_timeout_no_deadline_uses_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CLONE_TIMEOUT_SECONDS", "120")
    assert clone_timeout_for_deadline(None) == 120


def test_clone_timeout_clamps_to_remaining_minus_reserve() -> None:
    now = 1_000.0
    reserve = MIN_CODEX_CALL_SECONDS + call_margin_seconds_for_backend("in_process") + CLONE_CLEANUP_OVERHEAD_SECONDS
    deadline = JobDeadline(deadline_monotonic=now + 200)
    clamped = clone_timeout_for_deadline(deadline, configured=300, runner_backend="in_process", now=now)
    assert clamped == int(200 - reserve)
    assert clamped < 300


def test_clone_timeout_none_when_budget_too_small() -> None:
    now = 1_000.0
    deadline = JobDeadline(deadline_monotonic=now + 20)
    assert clone_timeout_for_deadline(deadline, configured=300, runner_backend="in_process", now=now) is None


def test_clone_timeout_container_reserves_launcher_overhead() -> None:
    now = 1_000.0
    reserve = MIN_CODEX_CALL_SECONDS + call_margin_seconds_for_backend("container") + CLONE_CLEANUP_OVERHEAD_SECONDS
    remaining = reserve + MIN_CLONE_SECONDS + 10
    deadline = JobDeadline(deadline_monotonic=now + remaining)
    clamped = clone_timeout_for_deadline(deadline, configured=300, runner_backend="container", now=now)
    assert clamped == MIN_CLONE_SECONDS + 10


@pytest.mark.asyncio
async def test_clone_head_raises_when_deadline_exhausted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    deadline = JobDeadline(deadline_monotonic=time.monotonic() + 10)
    from app.services.git_clone import clone_head

    with pytest.raises(CloneBudgetExhausted):
        await clone_head(
            owner="o",
            repo="r",
            sha="a" * 40,
            token="tok",
            job_deadline=deadline,
            runner_backend="in_process",
        )
    left = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert left == []


@pytest.mark.asyncio
async def test_clone_head_uses_clamped_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CLONE_TIMEOUT_SECONDS", "300")
    reserve = MIN_CODEX_CALL_SECONDS + call_margin_seconds_for_backend("in_process") + CLONE_CLEANUP_OVERHEAD_SECONDS
    remaining = reserve + 80
    deadline = JobDeadline(deadline_monotonic=time.monotonic() + remaining)
    seen_timeouts: list[float] = []

    async def fake_run(args, *, cwd, env, token, timeout_seconds=None):  # noqa: ANN001
        if timeout_seconds is not None:
            seen_timeouts.append(float(timeout_seconds))
        return None

    monkeypatch.setattr("app.services.git_clone._run", fake_run)
    monkeypatch.setattr("app.services.git_clone.neutralize_escaping_symlinks", lambda dest: 0)
    from app.services.git_clone import clone_head

    dest = await clone_head(
        owner="o",
        repo="r",
        sha="a" * 40,
        token="tok",
        job_deadline=deadline,
        runner_backend="in_process",
    )
    assert dest.is_dir()
    assert seen_timeouts, "git _run must receive a timeout"
    assert max(seen_timeouts) <= 80 + 1
    assert max(seen_timeouts) < 300
