"""Issue #29: whole launcher cancel stays under worker DELETE read timeout.

Delayed Docker list/remove use production timeout values so a slow daemon cannot
push cancel past ``WORKER_CANCEL_HTTP_READ_SECONDS``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.launcher.jobs import cancel_review_job
from app.launcher.timeouts import (
    LAUNCHER_CANCEL_DEADLINE_SECONDS,
    LAUNCHER_CANCEL_WAIT_SECONDS,
    WORKER_CANCEL_HTTP_READ_SECONDS,
    launcher_cancel_deadline_seconds,
    validate_cancel_timeouts,
)


def test_production_cancel_deadline_ordering() -> None:
    validate_cancel_timeouts()
    assert LAUNCHER_CANCEL_WAIT_SECONDS < LAUNCHER_CANCEL_DEADLINE_SECONDS < WORKER_CANCEL_HTTP_READ_SECONDS
    assert launcher_cancel_deadline_seconds() == LAUNCHER_CANCEL_DEADLINE_SECONDS == 4.0


def test_cancel_survives_slow_list_under_production_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list that would hang under the 60s client default still finishes under DELETE read."""
    jid = "a" * 32
    # Use production deadline via env (explicit).
    monkeypatch.delenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", raising=False)
    monkeypatch.delenv("LAUNCHER_CANCEL_WAIT_SECONDS", raising=False)
    validate_cancel_timeouts()

    calls: list[tuple[str, float | None]] = []
    # First force-remove list is slow but within deadline; second is empty/fast.
    list_delays = [1.2, 0.0, 0.0]  # kill-phase list, post-wait list, leftover list

    class SlowDocker:
        def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
            calls.append(("list", timeout))
            delay = list_delays.pop(0) if list_delays else 0.0
            # Honour the per-call budget: sleep only up to timeout (or a tiny slice).
            if timeout is not None:
                time.sleep(min(delay, max(0.0, timeout - 0.05)))
            else:
                time.sleep(delay)
            if delay >= 1.0:
                return [{"Id": "cidslowlist01", "Labels": {"opr.job_id": jid}}]
            return []

        def kill_container(self, cid: str, *, timeout: float | None = None) -> None:
            calls.append(("kill", timeout))

        def remove_container(self, cid: str, *, force: bool = True, timeout: float | None = None) -> None:
            calls.append(("remove", timeout))
            time.sleep(0.05)

        def close(self) -> None:
            return None

    started = time.monotonic()
    result = cancel_review_job(job_id=jid, client=SlowDocker())  # type: ignore[arg-type]
    elapsed = time.monotonic() - started

    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    assert elapsed < LAUNCHER_CANCEL_DEADLINE_SECONDS + 0.5  # small scheduling slack
    assert result["job_id"] == jid
    # Every Docker call received an explicit remaining-budget timeout.
    assert calls
    assert all(t is not None and t > 0 for _op, t in calls)
    assert any(op == "list" for op, _ in calls)
    assert any(op == "kill" for op, _ in calls)


def test_cancel_stops_when_budget_exhausted_by_slow_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow remove consumes the deadline; cancel returns without waiting on the 60s default."""
    jid = "b" * 32
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "1.2")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "0.5")
    # Re-import path uses runtime helpers; validate ordering for these values.
    from app.launcher.timeouts import launcher_cancel_deadline_seconds, launcher_cancel_wait_seconds

    assert launcher_cancel_wait_seconds() < launcher_cancel_deadline_seconds() < WORKER_CANCEL_HTTP_READ_SECONDS

    remove_sleeps = [0.9, 0.9]  # first remove eats most of the budget

    class ExhaustingDocker:
        def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
            assert timeout is not None and timeout > 0
            # Always claim one container so remove is attempted.
            return [{"Id": "cidexhaust01", "Labels": {"opr.job_id": jid}}]

        def kill_container(self, cid: str, *, timeout: float | None = None) -> None:
            assert timeout is not None and timeout > 0

        def remove_container(self, cid: str, *, force: bool = True, timeout: float | None = None) -> None:
            assert timeout is not None and timeout > 0
            delay = remove_sleeps.pop(0) if remove_sleeps else 0.05
            time.sleep(min(delay, max(0.0, (timeout or 0) - 0.02)))

        def close(self) -> None:
            return None

    started = time.monotonic()
    result = cancel_review_job(job_id=jid, client=ExhaustingDocker())  # type: ignore[arg-type]
    elapsed = time.monotonic() - started

    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    # Must not approach the old unbounded 60s client timeout.
    assert elapsed < 3.0
    assert result["job_id"] == jid
    # Budget exhausted before a definitive empty list → not confirmed.
    assert result["confirmed"] is False


def test_validate_cancel_rejects_deadline_above_worker_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "5.0")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "3.0")
    with pytest.raises(RuntimeError, match="LAUNCHER_CANCEL_DEADLINE_SECONDS"):
        validate_cancel_timeouts()


def test_validate_cancel_rejects_wait_above_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "2.0")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "2.5")
    with pytest.raises(RuntimeError, match="LAUNCHER_CANCEL_WAIT_SECONDS"):
        validate_cancel_timeouts()
