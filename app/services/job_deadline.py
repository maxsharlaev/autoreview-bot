"""ARQ job wall-clock budget for multi-call review jobs."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from app.launcher.timeouts import launcher_pipeline_overhead_seconds

# Leave headroom so the worker can finish cleanup before ARQ cancels the task.
JOB_DEADLINE_SAFETY_MARGIN_SECONDS = 30
# Per Codex call slack for in-process backend (no launcher HTTP wait).
CODEX_CALL_BUDGET_MARGIN_SECONDS = 30
# Refuse to start a call with less than this many seconds of effective timeout.
MIN_CODEX_CALL_SECONDS = 5


def call_margin_seconds_for_backend(runner_backend: str | None) -> int:
    """Seconds reserved beyond ``timeout_seconds`` for one Codex call.

    Container calls block until timeout + launcher pipeline overhead (HTTP read
    timeout); that full overhead must fit inside the remaining ARQ budget.
    """
    if (runner_backend or "").strip() == "container":
        return int(launcher_pipeline_overhead_seconds())
    return CODEX_CALL_BUDGET_MARGIN_SECONDS


@dataclass(frozen=True)
class JobDeadline:
    """Monotonic deadline derived once at ARQ job start (worker)."""

    deadline_monotonic: float
    safety_margin_seconds: float = JOB_DEADLINE_SAFETY_MARGIN_SECONDS
    call_margin_seconds: float = CODEX_CALL_BUDGET_MARGIN_SECONDS

    @classmethod
    def from_worker_job_timeout(
        cls,
        worker_job_timeout: int | float,
        *,
        now: float | None = None,
        safety_margin_seconds: float = JOB_DEADLINE_SAFETY_MARGIN_SECONDS,
        call_margin_seconds: float = CODEX_CALL_BUDGET_MARGIN_SECONDS,
    ) -> JobDeadline:
        started = time.monotonic() if now is None else float(now)
        limit = max(1, int(worker_job_timeout) - int(safety_margin_seconds))
        return cls(
            deadline_monotonic=started + float(limit),
            safety_margin_seconds=float(safety_margin_seconds),
            call_margin_seconds=float(call_margin_seconds),
        )

    def remaining(self, *, now: float | None = None) -> float:
        clock = time.monotonic() if now is None else float(now)
        return self.deadline_monotonic - clock

    def clamp_timeout(
        self,
        timeout_seconds: int,
        *,
        now: float | None = None,
        call_margin_seconds: int | float | None = None,
        runner_backend: str | None = None,
    ) -> int | None:
        """Return ``min(timeout, remaining - margin)``, or ``None`` if too little budget.

        For ``runner_backend=container``, margin defaults to the full launcher
        pipeline overhead so ``timeout + overhead`` never exceeds remaining budget
        (and thus the worker HTTP read deadline stays inside the ARQ job).
        """
        if call_margin_seconds is None:
            if runner_backend is not None:
                margin = float(call_margin_seconds_for_backend(runner_backend))
            else:
                margin = float(self.call_margin_seconds)
        else:
            margin = float(call_margin_seconds)
        usable = self.remaining(now=now) - margin
        usable_seconds = math.floor(usable + 1e-9)
        if usable_seconds < MIN_CODEX_CALL_SECONDS:
            return None
        return max(MIN_CODEX_CALL_SECONDS, min(int(timeout_seconds), usable_seconds))
