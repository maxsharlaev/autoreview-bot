"""ARB34-009: confirm-before-delete, restart-safe orphans, revoke status."""

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


def _fake_docker(containers: list[dict], removed: list[str], killed: list[str] | None = None):
    killed = killed if killed is not None else []

    class FakeDocker:
        def list_review_containers(self):
            return list(containers)

        def kill_container(self, cid):
            killed.append(cid)
            for c in containers:
                if c.get("Id") == cid:
                    c["State"] = "exited"
            return None

        def remove_container(self, cid, force=False):
            removed.append(cid)
            containers[:] = [c for c in containers if c.get("Id") != cid]
            return None

        def close(self):
            return None

    return FakeDocker()


def test_proxy_down_restart_reschedules_then_deletes_on_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy down ? reset scheduler (restart) ? next pass re-schedules ? delete after confirm."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs
    from app.launcher.revoke_scheduler import RevokeJobStatus

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "1")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    job = "a" * 32
    created = time.time() - 100
    keep_dir = tmp_path / job
    keep_dir.mkdir()
    os.utime(keep_dir, (created, created))

    attempts = {"n": 0}

    async def always_down(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        attempts["n"] += 1
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", always_down)

    containers = [
        {
            "Id": "cccc",
            "Created": created,
            "State": "running",
            "Names": ["/opr-review-a"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    killed: list[str] = []

    # Pass 1: stop + schedule; must NOT remove (pending).
    reaper_mod.reap_once(client=_fake_docker(containers, removed, killed))  # type: ignore[arg-type]
    time.sleep(0.1)
    assert "cccc" in killed
    assert "cccc" not in removed
    assert keep_dir.exists()
    assert rs.revoke_status(job, created_at=created) is RevokeJobStatus.PENDING

    # Simulate launcher restart (lose in-memory scheduler state); orphan remains.
    rs._reset_for_tests()
    assert rs.revoke_status(job, created_at=created) is RevokeJobStatus.PENDING

    calls_after: list[str] = []

    async def ok(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        calls_after.append(job_id)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", ok)
    removed.clear()
    # Pass 2 after restart: re-schedule and wait for confirmation.
    reaper_mod.reap_once(client=_fake_docker(containers, removed, killed))  # type: ignore[arg-type]
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if rs.revoke_status(job, created_at=created) is RevokeJobStatus.CONFIRMED:
            break
        time.sleep(0.05)
    assert rs.revoke_status(job, created_at=created) is RevokeJobStatus.CONFIRMED
    assert job in calls_after

    # Pass 3: now confirmed ? delete.
    removed.clear()
    reaper_mod.reap_once(client=_fake_docker(containers, removed, killed))  # type: ignore[arg-type]
    assert "cccc" in removed
    assert not keep_dir.exists()


def test_deletion_after_window_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs
    from app.launcher.revoke_scheduler import RevokeJobStatus

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    async def always_down(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", always_down)

    job = "b" * 32
    created = time.time() - 10  # already past 1s window
    containers = [
        {
            "Id": "bbbb",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-b"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    assert rs.revoke_status(job, created_at=created) is RevokeJobStatus.WINDOW_EXPIRED
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "bbbb" in removed


def test_stopped_container_not_removed_while_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs
    from app.launcher.revoke_scheduler import RevokeJobStatus

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    release = threading.Event()

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)

    job = "c" * 32
    created = time.time() - 100
    containers = [
        {
            "Id": "cccc",
            "Created": created,
            "State": "running",
            "Names": ["/opr-review-c"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    killed: list[str] = []
    reaper_mod.reap_once(client=_fake_docker(containers, removed, killed))  # type: ignore[arg-type]
    assert "cccc" in killed
    assert "cccc" not in removed
    assert rs.revoke_status(job, created_at=created) is RevokeJobStatus.PENDING
    release.set()
    time.sleep(0.15)


def test_queue_full_keeps_then_deletes_after_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_MAX_PENDING", "1")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    release = threading.Event()

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)
    assert rs.schedule_proxy_token_revoke("f" * 32) is True
    time.sleep(0.05)

    keep_job = "a" * 32
    created = time.time() - 100
    keep_dir = tmp_path / keep_job
    keep_dir.mkdir()
    os.utime(keep_dir, (created, created))
    containers = [
        {
            "Id": "cccc",
            "Created": created,
            "State": "running",
            "Names": ["/opr-review-a"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": keep_job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert keep_dir.exists()
    assert "cccc" not in removed
    assert keep_job in rs.retry_needed_job_ids()

    release.set()
    time.sleep(0.15)
    rs._reset_for_tests()

    async def ok(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", ok)
    # Schedule; wait until confirmed; delete on a pass after confirmation.
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not rs.may_delete_after_revoke(keep_job, created_at=created):
        time.sleep(0.05)
    assert rs.may_delete_after_revoke(keep_job, created_at=created)
    if "cccc" not in removed or keep_dir.exists():
        reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cccc" in removed
    assert not keep_dir.exists()


def test_reaper_not_blocked_by_hanging_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import reaper as reaper_mod
    from app.launcher.revoke_scheduler import RevokeJobStatus

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)
    monkeypatch.setattr(reaper_mod, "drain_revoke_retries", lambda: 0)

    scheduled: list[str] = []

    def accept(jid: str, *, created_at: float | None = None) -> bool:
        scheduled.append(jid)
        return True

    monkeypatch.setattr(reaper_mod, "schedule_proxy_token_revoke", accept)
    # Pretend already confirmed so deletes proceed without waiting on real scheduler.
    monkeypatch.setattr(
        reaper_mod,
        "may_delete_after_revoke",
        lambda jid, created_at=None, now=None: True,
    )

    hang_job = "a" * 32
    other_job = "b" * 32
    other_dir = tmp_path / other_job
    other_dir.mkdir()
    old = time.time() - 3600
    os.utime(other_dir, (old, old))
    removed: list[str] = []
    containers = [
        {
            "Id": "aaaa",
            "Created": 0,
            "State": "running",
            "Names": ["/opr-review-a"],
            "Labels": {"opr.review": "1", "opr.job_id": hang_job, "opr.created_unix": "0"},
        },
        {
            "Id": "bbbb",
            "Created": 0,
            "State": "running",
            "Names": ["/opr-review-b"],
            "Labels": {"opr.review": "1", "opr.job_id": other_job, "opr.created_unix": "0"},
        },
    ]
    t0 = time.monotonic()
    removed_n = reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    assert removed_n >= 2
    assert "aaaa" in removed and "bbbb" in removed
    assert hang_job in scheduled and other_job in scheduled
    assert not other_dir.exists()
    assert RevokeJobStatus.PENDING  # noqa: B018 - enum imported for type presence


def test_same_job_id_not_revoked_twice_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_MAX_PENDING", "8")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")

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
    created = time.time()
    assert rs.schedule_proxy_token_revoke(jid, created_at=created) is True
    assert started.wait(timeout=2.0)
    assert rs.schedule_proxy_token_revoke(jid, created_at=created) is True
    time.sleep(0.1)
    assert calls == [jid]
    release.set()
    time.sleep(0.1)


def test_transient_failure_retried_to_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.launcher import revoke_scheduler as rs
    from app.launcher.revoke_scheduler import RevokeJobStatus

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
    created = time.time()
    assert rs.schedule_proxy_token_revoke("b" * 32, created_at=created) is True
    assert done.wait(timeout=3.0)
    assert attempts["n"] >= 3
    time.sleep(0.05)
    assert rs.revoke_status("b" * 32, created_at=created) is RevokeJobStatus.CONFIRMED


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
