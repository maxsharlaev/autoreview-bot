"""Per-job launch lifecycle for cancel races (thread-safe, in-memory)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.launcher.timeouts import LAUNCHER_CANCEL_WAIT_SECONDS

logger = logging.getLogger(__name__)

DEFAULT_TOMBSTONE_TTL_SECONDS = 600


class JobPhase(StrEnum):
    ACCEPTED = "accepted"
    CREATING = "creating"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass
class JobRecord:
    phase: JobPhase
    container_id: str = ""
    # True from accept() until mark_finished(); cancel waits on this.
    launch_active: bool = False
    updated_monotonic: float = field(default_factory=time.monotonic)
    tombstone_until: float = 0.0


class JobLifecycleRegistry:
    """In-memory map of job_id -> launch phase for cancel coordination."""

    def __init__(
        self,
        *,
        tombstone_ttl_seconds: float = DEFAULT_TOMBSTONE_TTL_SECONDS,
        cancel_wait_seconds: float | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._cond = threading.Condition(self._lock)
        self._tombstone_ttl = float(tombstone_ttl_seconds)
        self._cancel_wait = float(LAUNCHER_CANCEL_WAIT_SECONDS if cancel_wait_seconds is None else cancel_wait_seconds)

    def _purge_expired_tombstones(self, now: float) -> None:
        expired = [
            jid
            for jid, rec in self._jobs.items()
            if rec.phase == JobPhase.CANCELLED
            and not rec.launch_active
            and rec.tombstone_until
            and rec.tombstone_until <= now
        ]
        for jid in expired:
            self._jobs.pop(jid, None)

    def accept(self, job_id: str) -> None:
        """Begin a launch. Raises RuntimeError if a cancel tombstone is present."""
        with self._cond:
            now = time.monotonic()
            self._purge_expired_tombstones(now)
            rec = self._jobs.get(job_id)
            if rec is not None and rec.phase == JobPhase.CANCELLED:
                raise RuntimeError(f"job {job_id} was cancelled")
            if rec is not None and rec.launch_active:
                raise RuntimeError(f"job {job_id} already in flight ({rec.phase.value})")
            self._jobs[job_id] = JobRecord(
                phase=JobPhase.ACCEPTED,
                launch_active=True,
                updated_monotonic=now,
            )

    def mark_creating(self, job_id: str) -> None:
        with self._cond:
            rec = self._require_active(job_id)
            self._raise_if_cancelled(rec, job_id)
            rec.phase = JobPhase.CREATING
            rec.updated_monotonic = time.monotonic()

    def mark_running(self, job_id: str, container_id: str) -> None:
        with self._cond:
            rec = self._require_active(job_id)
            self._raise_if_cancelled(rec, job_id)
            rec.phase = JobPhase.RUNNING
            rec.container_id = container_id
            rec.updated_monotonic = time.monotonic()
            self._cond.notify_all()

    def mark_finished(self, job_id: str, *, cancelled: bool = False) -> None:
        """End the launch (success, failure, or observed cancel)."""
        with self._cond:
            rec = self._jobs.get(job_id)
            if rec is None:
                return
            if cancelled or rec.phase == JobPhase.CANCELLED:
                rec.phase = JobPhase.CANCELLED
                if not rec.tombstone_until:
                    rec.tombstone_until = time.monotonic() + self._tombstone_ttl
            else:
                rec.phase = JobPhase.DONE
            rec.launch_active = False
            rec.container_id = ""
            rec.updated_monotonic = time.monotonic()
            self._cond.notify_all()

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            rec = self._jobs.get(job_id)
            return rec is not None and rec.phase == JobPhase.CANCELLED

    def check_not_cancelled(self, job_id: str) -> None:
        if self.is_cancelled(job_id):
            raise RuntimeError(f"job {job_id} was cancelled")

    def request_cancel(self, job_id: str) -> JobRecord:
        """Mark cancelled; create a tombstone when the job is unknown."""
        with self._cond:
            now = time.monotonic()
            self._purge_expired_tombstones(now)
            rec = self._jobs.get(job_id)
            if rec is None:
                rec = JobRecord(
                    phase=JobPhase.CANCELLED,
                    launch_active=False,
                    updated_monotonic=now,
                    tombstone_until=now + self._tombstone_ttl,
                )
                self._jobs[job_id] = rec
            else:
                rec.phase = JobPhase.CANCELLED
                rec.tombstone_until = now + self._tombstone_ttl
                rec.updated_monotonic = now
            self._cond.notify_all()
            return JobRecord(
                phase=rec.phase,
                container_id=rec.container_id,
                launch_active=rec.launch_active,
                updated_monotonic=rec.updated_monotonic,
                tombstone_until=rec.tombstone_until,
            )

    def wait_until_launch_idle(self, job_id: str, *, timeout: float | None = None) -> bool:
        """Wait until no launch is active for ``job_id``. Returns True if idle."""
        limit = self._cancel_wait if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        with self._cond:
            while True:
                rec = self._jobs.get(job_id)
                if rec is None or not rec.launch_active:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return not rec.launch_active
                self._cond.wait(timeout=remaining)

    def launch_active(self, job_id: str) -> bool:
        with self._lock:
            rec = self._jobs.get(job_id)
            return bool(rec and rec.launch_active)

    def _require_active(self, job_id: str) -> JobRecord:
        rec = self._jobs.get(job_id)
        if rec is None or not rec.launch_active:
            raise RuntimeError(f"job {job_id} not in an active launch")
        return rec

    @staticmethod
    def _raise_if_cancelled(rec: JobRecord, job_id: str) -> None:
        if rec.phase == JobPhase.CANCELLED:
            raise RuntimeError(f"job {job_id} was cancelled")


lifecycle = JobLifecycleRegistry()
