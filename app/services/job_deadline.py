"""ARQ job wall-clock budget for multi-call review jobs."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

# Leave headroom so the worker can finish cleanup before ARQ cancels the task.
JOB_DEADLINE_SAFETY_MARGIN_SECONDS = 30
# Per Codex call: HTTP/setup/teardown slack subtracted from remaining budget.
CODEX_CALL_BUDGET_MARGIN_SECONDS = 30
# Refuse to start a call with less than this many seconds of effective timeout.
MIN_CODEX_CALL_SECONDS = 5


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
        # Integer arithmetic avoids float drift when tests inject ``now``.
        limit = max(1, int(worker_job_timeout) - int(safety_margin_seconds))
        return cls(
            deadline_monotonic=started + float(limit),
            safety_margin_seconds=float(safety_margin_seconds),
            call_margin_seconds=float(call_margin_seconds),
        )

    def remaining(self, *, now: float | None = None) -> float:
        clock = time.monotonic() if now is None else float(now)
        return self.deadline_monotonic - clock

    def clamp_timeout(self, timeout_seconds: int, *, now: float | None = None) -> int | None:
        """Return ``min(timeout, remaining - call_margin)``, or ``None`` if too little budget."""
        usable = self.remaining(now=now) - self.call_margin_seconds
        # Tolerate float noise so ``now`` round-trips stay deterministic.
        usable_seconds = math.floor(usable + 1e-9)
        if usable_seconds < MIN_CODEX_CALL_SECONDS:
            return None
        return max(MIN_CODEX_CALL_SECONDS, min(int(timeout_seconds), usable_seconds))
