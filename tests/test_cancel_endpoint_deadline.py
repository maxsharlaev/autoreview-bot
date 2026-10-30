"""AR-030-004: DELETE cancel returns before worker read timeout even if Docker stalls."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from app.launcher.job_lifecycle import lifecycle
from app.launcher.timeouts import (
    WORKER_CANCEL_HTTP_READ_SECONDS,
    launcher_cancel_deadline_seconds,
)
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def cancel_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token = "test-launcher-token-32chars-ok"
    monkeypatch.setenv("LAUNCHER_TOKEN", token)
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "0.8")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "0.3")

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


def test_delete_returns_unconfirmed_when_cancel_work_blocks(
    cancel_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint must not wait forever for stuck cancel work (AR-030-004)."""
    client, token = cancel_app
    jid = "a" * 32
    started_work = threading.Event()
    release_work = threading.Event()

    def blocking_cancel(*, job_id: str, client: Any | None = None) -> dict:
        started_work.set()
        assert release_work.wait(timeout=30), "test release timed out"
        return {"job_id": job_id, "removed": 1, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", blocking_cancel)

    started = time.monotonic()
    response = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == jid
    assert body["confirmed"] is False
    assert body["removed"] == 0
    assert elapsed < launcher_cancel_deadline_seconds() + 0.75
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    assert started_work.wait(timeout=2)
    assert lifecycle.is_cancelled(jid)
    release_work.set()
    # Give the pool thread a moment to finish after release.
    time.sleep(0.05)


def test_delete_returns_before_worker_timeout_on_blocked_dns(
    cancel_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked getaddrinfo must not hold DELETE past the cancel deadline."""
    client, token = cancel_app
    jid = "b" * 32
    monkeypatch.setenv("DOCKER_HOST", "http://dns-stall.invalid.example:2375")

    release = threading.Event()

    def blocking_getaddrinfo(*_args, **_kwargs):
        release.wait(timeout=60)
        raise socket.gaierror("intentionally stalled resolver")

    monkeypatch.setattr(socket, "getaddrinfo", blocking_getaddrinfo)

    started = time.monotonic()
    response = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert response.json()["confirmed"] is False
    assert elapsed < launcher_cancel_deadline_seconds() + 0.75
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    assert lifecycle.is_cancelled(jid)
    release.set()


def test_duplicate_delete_coalesces_inflight_cancel(
    cancel_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, token = cancel_app
    jid = "c" * 32
    calls = {"n": 0}
    gate = threading.Event()

    def slow_cancel(*, job_id: str, client: Any | None = None) -> dict:
        calls["n"] += 1
        gate.wait(timeout=5)
        return {"job_id": job_id, "removed": 0, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", slow_cancel)
    # Longer deadline so both waiters can finish after gate opens.
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "2.0")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "0.5")

    results: list[dict] = []
    errors: list[BaseException] = []

    def _hit() -> None:
        try:
            r = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
            results.append(r.json())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_hit)
    t2 = threading.Thread(target=_hit)
    t1.start()
    time.sleep(0.05)
    t2.start()
    time.sleep(0.1)
    gate.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not errors
    assert len(results) == 2
    assert calls["n"] == 1
    assert all(r.get("confirmed") is True for r in results)


def test_background_completion_after_unconfirmed_response(
    cancel_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After DELETE returns unconfirmed, background cancel may still finish."""
    client, token = cancel_app
    jid = "d" * 32
    finished = threading.Event()

    def delayed_cancel(*, job_id: str, client: Any | None = None) -> dict:
        time.sleep(1.2)  # longer than fixture deadline 0.8s
        finished.set()
        return {"job_id": job_id, "removed": 0, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", delayed_cancel)

    response = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["confirmed"] is False
    assert lifecycle.is_cancelled(jid)
    assert finished.wait(timeout=5), "background cancel did not complete"
