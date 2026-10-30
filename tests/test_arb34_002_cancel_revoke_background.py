"""ARB34-002: cancel DELETE must not wait on slow proxy token revoke."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
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
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16ok!!")
    monkeypatch.setenv("MODEL_PROXY_URL", "http://127.0.0.1:9")  # immediate refuse

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


def test_slow_proxy_revoke_does_not_delay_cancel_response(
    cancel_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARB34-002: hung revoke must not push DELETE past the cancel deadline."""
    client, token = cancel_app
    jid = "e" * 32
    revoke_started = threading.Event()
    revoke_released = threading.Event()
    revoke_calls: list[str] = []

    def fast_cancel(*, job_id: str, client: Any | None = None) -> dict:
        return {"job_id": job_id, "removed": 1, "confirmed": True}

    def hanging_revoke(*, job_id: str, **_kwargs: Any) -> bool:
        revoke_calls.append(job_id)
        revoke_started.set()
        # Hang longer than the cancel deadline + worker margin.
        assert revoke_released.wait(timeout=30), "test release timed out"
        return True

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", fast_cancel)
    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_sync",
        hanging_revoke,
    )

    started = time.monotonic()
    response = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == jid
    assert body["confirmed"] is True
    # Response must finish well under worker DELETE read (5s) and near deadline.
    assert elapsed < launcher_cancel_deadline_seconds() + 0.75
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    # Revoke was scheduled in background (may start slightly after return).
    assert revoke_started.wait(timeout=2.0), "background revoke never started"
    assert revoke_calls == [jid]
    revoke_released.set()
    time.sleep(0.05)


def test_cancel_review_job_does_not_call_revoke_inline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct cancel_review_job no longer blocks on proxy revoke (ARB34-002)."""
    from app.launcher.jobs import cancel_review_job

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    revoke_called = {"n": 0}

    def boom_revoke(**_kwargs: Any) -> bool:
        revoke_called["n"] += 1
        raise AssertionError("revoke must not run inline in cancel_review_job")

    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_sync",
        boom_revoke,
    )

    class FakeDocker:
        def list_containers_for_job(self, job_id: str, timeout: float | None = None):
            return []

        def kill_container(self, *args, **kwargs):
            return None

        def remove_container(self, *args, **kwargs):
            return None

        def close(self) -> None:
            return None

    result = cancel_review_job(job_id="f" * 32, client=FakeDocker())  # type: ignore[arg-type]
    assert result["confirmed"] is True
    assert revoke_called["n"] == 0
