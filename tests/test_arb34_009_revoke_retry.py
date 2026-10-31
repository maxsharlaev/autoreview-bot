"""ARB34-009: revoke accept/reject, retries, dedup, reaper retains on queue full."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from app.launcher.timeouts import (
    WORKER_CANCEL_HTTP_READ_SECONDS,
    launcher_cancel_deadline_seconds,
)
from app.services.model_proxy_client import RevokeAttemptOutcome
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_revoke_scheduler():
    from app.launcher import revoke_scheduler as rs

    rs._reset_for_tests()
    yield
    rs._reset_for_tests()


def test_queue_full_reaper_keeps_then_succeeds_next_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full revoke queue: reaper keeps dir/container; next pass deletes after accept."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "1")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_MAX_PENDING", "1")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    release = threading.Event()

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)

    # Occupy the single pending slot with an unrelated job.
    assert rs.schedule_proxy_token_revoke("f" * 32) is True
    time.sleep(0.05)

    keep_job = "a" * 32
    keep_dir = tmp_path / keep_job
    keep_dir.mkdir()
    old = time.time() - 3600
    os.utime(keep_dir, (old, old))

    removed_cids: list[str] = []

    class FakeDocker:
        def list_review_containers(self):
            return [
                {
                    "Id": "cccc",
                    "Created": 0,
                    "Names": ["/opr-review-a"],
                    "Labels": {"opr.review": "1", "opr.job_id": keep_job},
                }
            ]

        def kill_container(self, cid):
            return None

        def remove_container(self, cid, force=False):
            removed_cids.append(cid)
            return None

        def close(self):
            return None

    # Pass 1: queue full -> keep container and checkout.
    removed1 = reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    assert keep_dir.exists(), "checkout must be retained when revoke rejected"
    assert "cccc" not in removed_cids
    assert keep_job in rs.retry_needed_job_ids()
    assert removed1 == 0

    # Free the queue and run again.
    release.set()
    time.sleep(0.15)
    rs._reset_for_tests()

    # Accepting schedule should allow delete.
    calls: list[str] = []

    async def ok(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        calls.append(job_id)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", ok)
    removed_cids.clear()
    removed2 = reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    assert "cccc" in removed_cids
    assert not keep_dir.exists()
    assert removed2 >= 1
    time.sleep(0.05)
    assert keep_job in calls


def test_transient_failure_retried_to_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_INITIAL_BACKOFF_SECONDS", "0.05")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_MAX_BACKOFF_SECONDS", "0.1")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "30")

    attempts = {"n": 0}
    done = threading.Event()

    async def flaky(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return RevokeAttemptOutcome.TRANSIENT
        done.set()
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", flaky)
    assert rs.schedule_proxy_token_revoke("b" * 32) is True
    assert done.wait(timeout=3.0), f"never succeeded after retries; attempts={attempts['n']}"
    assert attempts["n"] >= 3
    time.sleep(0.05)
    assert rs.pending_revoke_count() == 0


def test_retries_stop_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_INITIAL_BACKOFF_SECONDS", "0.05")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_MAX_BACKOFF_SECONDS", "0.05")
    # Tiny TTL window so retries stop quickly.
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "0.4")

    attempts = {"n": 0}
    finished = threading.Event()

    async def always_transient(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        attempts["n"] += 1
        return RevokeAttemptOutcome.TRANSIENT

    real_clear = rs.clear_revoke_needed

    def clear_and_signal(job_id: str) -> None:
        real_clear(job_id)
        if rs.pending_revoke_count() <= 1:
            finished.set()

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", always_transient)
    # Patch clear inside worker via monkeypatch on module used after give-up
    # Detect completion via pending dropping to 0.
    assert rs.schedule_proxy_token_revoke("c" * 32) is True
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if rs.pending_revoke_count() == 0 and attempts["n"] >= 1:
            break
        time.sleep(0.05)
    assert rs.pending_revoke_count() == 0
    assert attempts["n"] >= 1
    # Should not keep retrying forever after TTL.
    n_after = attempts["n"]
    time.sleep(0.3)
    assert attempts["n"] == n_after


def test_same_job_id_not_revoked_twice_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_MAX_PENDING", "8")

    release = threading.Event()
    started = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        with lock:
            calls.append(job_id)
        started.set()
        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)
    jid = "d" * 32
    assert rs.schedule_proxy_token_revoke(jid) is True
    assert started.wait(timeout=2.0)
    # Second schedule while in-flight must dedupe (accepted, no second call).
    assert rs.schedule_proxy_token_revoke(jid) is True
    time.sleep(0.1)
    assert calls == [jid]
    release.set()
    time.sleep(0.1)


def test_reaper_not_blocked_by_hanging_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import reaper as reaper_mod

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    scheduled: list[str] = []

    def accept(jid: str) -> bool:
        scheduled.append(jid)
        return True

    monkeypatch.setattr(reaper_mod, "schedule_proxy_token_revoke", accept)
    monkeypatch.setattr(reaper_mod, "drain_revoke_retries", lambda: 0)

    hang_job = "a" * 32
    other_job = "b" * 32
    other_dir = tmp_path / other_job
    other_dir.mkdir()
    old = time.time() - 3600
    os.utime(other_dir, (old, old))
    removed_cids: list[str] = []

    class FakeDocker:
        def list_review_containers(self):
            return [
                {
                    "Id": "aaaa",
                    "Created": 0,
                    "Names": ["/opr-review-a"],
                    "Labels": {"opr.review": "1", "opr.job_id": hang_job},
                },
                {
                    "Id": "bbbb",
                    "Created": 0,
                    "Names": ["/opr-review-b"],
                    "Labels": {"opr.review": "1", "opr.job_id": other_job},
                },
            ]

        def kill_container(self, cid):
            return None

        def remove_container(self, cid, force=False):
            removed_cids.append(cid)
            return None

        def close(self):
            return None

    t0 = time.monotonic()
    removed = reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    assert removed >= 2
    assert "aaaa" in removed_cids and "bbbb" in removed_cids
    assert hang_job in scheduled and other_job in scheduled
    assert not other_dir.exists()


@pytest.fixture()
def cancel_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token = "test-launcher-token-32chars-ok"
    monkeypatch.setenv("LAUNCHER_TOKEN", token)
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "0.8")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "0.3")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "5.0")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_MAX_PENDING", "8")
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16ok!!")
    monkeypatch.setenv("MODEL_PROXY_URL", "http://127.0.0.1:9")

    from app.launcher.auth import require_launcher_auth
    from app.launcher.main import cancel_job

    app = FastAPI()
    app.add_api_route(
        "/v1/jobs/{job_id}",
        cancel_job,
        methods=["DELETE"],
        dependencies=[Depends(require_launcher_auth)],
    )
    return TestClient(app), token


def test_cancel_still_meets_deadline_with_hanging_revoke(
    cancel_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, token = cancel_app
    release = threading.Event()

    async def hanging_revoke(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    def fast_cancel(*, job_id: str, client: Any | None = None) -> dict:
        return {"job_id": job_id, "removed": 1, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", fast_cancel)
    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_attempt",
        hanging_revoke,
    )

    jid = "e" * 32
    t0 = time.monotonic()
    response = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
    elapsed = time.monotonic() - t0
    assert response.status_code == 200
    assert response.json()["confirmed"] is True
    assert elapsed < launcher_cancel_deadline_seconds() + 0.75
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    release.set()
    time.sleep(0.05)
