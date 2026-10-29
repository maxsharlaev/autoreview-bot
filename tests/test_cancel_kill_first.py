"""ARB-27-014: cancel kills immediately; launcher wait < worker DELETE read."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from app.launcher.job_lifecycle import JobLifecycleRegistry
from app.launcher.jobs import cancel_review_job
from app.launcher.timeouts import (
    LAUNCHER_CANCEL_WAIT_SECONDS,
    WORKER_CANCEL_HTTP_READ_SECONDS,
    validate_cancel_timeouts,
)


def test_production_cancel_timeout_ordering() -> None:
    validate_cancel_timeouts()
    assert LAUNCHER_CANCEL_WAIT_SECONDS < WORKER_CANCEL_HTTP_READ_SECONDS
    assert LAUNCHER_CANCEL_WAIT_SECONDS == 3.0
    assert WORKER_CANCEL_HTTP_READ_SECONDS == 5.0


def test_cancel_kills_before_wait_with_running_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a running container, cancel must kill immediately (not wait first)."""
    jid = "f" * 32
    reg = JobLifecycleRegistry(cancel_wait_seconds=LAUNCHER_CANCEL_WAIT_SECONDS)
    monkeypatch.setattr("app.launcher.jobs.lifecycle", reg)
    reg.accept(jid)
    reg.mark_creating(jid)
    reg.mark_running(jid, "cid-running")

    events: list[str] = []
    containers = [{"Id": "cid-running", "Labels": {"opr.job_id": jid}}]
    release_finish = threading.Event()

    class FakeDocker:
        def list_containers_for_job(self, job_id: str) -> list[dict[str, Any]]:
            events.append("list")
            return list(containers)

        def kill_container(self, cid: str) -> None:
            events.append(f"kill:{cid}")
            containers.clear()

        def remove_container(self, cid: str, *, force: bool = True) -> None:
            events.append(f"remove:{cid}")
            containers.clear()

        def close(self) -> None:
            return None

    def finish_later() -> None:
        # Simulate launch noticing kill and finishing after a short delay.
        assert release_finish.wait(timeout=5)
        time.sleep(0.05)
        reg.mark_finished(jid, cancelled=True)

    thread = threading.Thread(target=finish_later)
    thread.start()

    # Unblock finish shortly after cancel starts killing.
    def release_soon() -> None:
        time.sleep(0.02)
        release_finish.set()

    threading.Thread(target=release_soon).start()
    started = time.monotonic()
    result = cancel_review_job(job_id=jid, client=FakeDocker())  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    thread.join(timeout=5)
    assert result["confirmed"] is True
    # Kill happened before wait completed (first actions include kill).
    assert events[0] == "list"
    assert any(e.startswith("kill:") for e in events)
    assert events.index("kill:cid-running") < len(events)
    # Must finish well under worker DELETE read timeout.
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
