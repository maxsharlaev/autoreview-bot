"""ARB34-015: attempt deadline ≠ token deadline; cancel passes expires_at."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from app.launcher.revoke_scheduler import RevokeJobStatus
from app.services.model_proxy_client import RevokeAttemptOutcome


@pytest.fixture(autouse=True)
def _reset_revoke_scheduler():
    from app.launcher import revoke_scheduler as rs

    rs._reset_for_tests()
    yield
    rs._reset_for_tests()


def test_attempt_exhaust_without_expires_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short attempt bound must not become WINDOW_EXPIRED (ARB34-015)."""
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "0.2")

    async def always_down(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_attempt",
        always_down,
    )

    job = "a" * 32
    assert rs.schedule_proxy_token_revoke(job) is True
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        st = rs.revoke_status(job)
        if st is RevokeJobStatus.PENDING and job not in rs._ACTIVE_JOBS:
            # attempt finished; must not be WINDOW_EXPIRED
            break
        time.sleep(0.05)
    st = rs.revoke_status(job)
    assert st is RevokeJobStatus.PENDING
    assert st is not RevokeJobStatus.WINDOW_EXPIRED


def test_later_expires_at_upgrades_erroneous_window_expired() -> None:
    from app.launcher import revoke_scheduler as rs

    job = "b" * 32
    rs._STATUS[job] = RevokeJobStatus.WINDOW_EXPIRED
    future = time.time() + 3600
    st = rs.revoke_status(job, expires_at=future)
    assert st is RevokeJobStatus.PENDING
    # may_delete must not allow delete while pending with future expires
    assert rs.may_delete_after_revoke(job, expires_at=future) is False


def test_upgraded_status_deletes_only_after_real_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("LAUNCHER_REVOKE_CLOCK_SKEW_SECONDS", "0")
    job = "c" * 32
    rs._STATUS[job] = RevokeJobStatus.WINDOW_EXPIRED
    past = time.time() - 10
    future = time.time() + 3600
    # Later (more conservative) deadline wins and reopens PENDING.
    assert rs.revoke_status(job, expires_at=past) is RevokeJobStatus.WINDOW_EXPIRED
    assert rs.revoke_status(job, expires_at=future) is RevokeJobStatus.PENDING
    assert rs.may_delete_after_revoke(job, expires_at=future) is False
    # After the real deadline (+skew 0), window expires and delete is allowed.
    assert rs.may_delete_after_revoke(job, expires_at=past, now=time.time() + 4000) is True


def test_cancel_with_proxy_down_then_expires_keeps_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel schedules without expiry; later expires_at from job.json retains evidence."""
    from app.launcher import cancel_scheduler as cs
    from app.launcher import revoke_scheduler as rs
    from app.launcher.job_lifecycle import lifecycle

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "1")

    job = "d" * 32
    job_dir = tmp_path / job
    job_dir.mkdir()
    # No token_expires_unix yet (unknown at cancel time).

    async def always_down(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_attempt",
        always_down,
    )

    # Skip real Docker cancel work.
    monkeypatch.setattr(
        "app.launcher.jobs.cancel_review_job",
        lambda **_k: {"job_id": job, "removed": 0, "confirmed": True},
    )
    monkeypatch.setattr(lifecycle, "request_cancel", lambda jid: None)

    result = cs.cancel_review_job_bounded(job_id=job)
    assert result["job_id"] == job

    # Wait for short attempt to finish.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and job in rs._ACTIVE_JOBS:
        time.sleep(0.05)
    assert rs.revoke_status(job) is RevokeJobStatus.PENDING

    # Later: job.json gains expires_at (or reaper discovers label).
    future = time.time() + 3600
    (job_dir / "job.json").write_text(
        json.dumps({"token_expires_unix": int(future)}),
        encoding="utf-8",
    )
    st = rs.revoke_status(job, expires_at=future)
    assert st is RevokeJobStatus.PENDING
    assert rs.may_delete_after_revoke(job, expires_at=future) is False


def test_cancel_passes_expires_from_job_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import cancel_scheduler as cs
    from app.launcher.job_lifecycle import lifecycle

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "1")

    job = "e" * 32
    job_dir = tmp_path / job
    job_dir.mkdir()
    future = time.time() + 7200
    (job_dir / "job.json").write_text(
        json.dumps({"token_expires_unix": int(future)}),
        encoding="utf-8",
    )

    seen: dict[str, Any] = {}

    def capture(jid: str, **kwargs: Any) -> bool:
        seen["jid"] = jid
        seen["kwargs"] = kwargs
        return True

    monkeypatch.setattr(cs, "schedule_proxy_token_revoke", capture)
    monkeypatch.setattr(
        "app.launcher.jobs.cancel_review_job",
        lambda **_k: {"job_id": job, "removed": 0, "confirmed": True},
    )
    monkeypatch.setattr(lifecycle, "request_cancel", lambda jid: None)

    cs.cancel_review_job_bounded(job_id=job)
    assert seen["jid"] == job
    assert seen["kwargs"].get("expires_at") == pytest.approx(float(int(future)), abs=1.0)


def test_cancel_deadline_still_met_with_hanging_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded cancel must return within deadline even if revoke hangs."""
    from app.launcher import cancel_scheduler as cs
    from app.launcher.job_lifecycle import lifecycle

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "0.3")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "5")

    job = "f" * 32
    (tmp_path / job).mkdir()

    def slow_cancel(**_k: Any) -> dict:
        time.sleep(2.0)
        return {"job_id": job, "removed": 0, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", slow_cancel)
    monkeypatch.setattr(lifecycle, "request_cancel", lambda jid: None)

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        await __import__("asyncio").sleep(30)
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_attempt",
        hang,
    )

    started = time.monotonic()
    result = cs.cancel_review_job_bounded(job_id=job)
    elapsed = time.monotonic() - started
    assert result["confirmed"] is False
    assert elapsed < 1.5
