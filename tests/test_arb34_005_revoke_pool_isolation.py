"""ARB34-005: blocked proxy revokes must not starve Docker cancel on _CANCEL_POOL."""

from __future__ import annotations

import asyncio
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


def test_blocked_revokes_do_not_starve_cancel(
    cancel_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four+ hanging revokes must not delay a subsequent DELETE cancel response."""
    client, token = cancel_app
    release = threading.Event()
    started = threading.Event()
    revoke_holds = 0
    lock = threading.Lock()

    async def hanging_revoke(*, job_id: str, **_kwargs: Any):
        from app.services.model_proxy_client import RevokeAttemptOutcome

        nonlocal revoke_holds
        with lock:
            revoke_holds += 1
            if revoke_holds >= 4:
                started.set()
        # Block until test releases (longer than cancel deadline).
        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    def fast_cancel(*, job_id: str, client: Any | None = None) -> dict:
        return {"job_id": job_id, "removed": 1, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", fast_cancel)
    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_attempt",
        hanging_revoke,
    )

    # Saturate revoke path with 4+ cancels that finish Docker work instantly.
    for i in range(5):
        jid = f"{i:032x}"
        response = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        assert response.json()["confirmed"] is True

    assert started.wait(timeout=3.0), "revokes never held the dedicated loop"

    # A further cancel must still return under the cancel deadline (cancel pool free).
    jid = "a" * 32
    t0 = time.monotonic()
    response = client.delete(f"/v1/jobs/{jid}", headers={"Authorization": f"Bearer {token}"})
    elapsed = time.monotonic() - t0
    assert response.status_code == 200
    assert response.json()["confirmed"] is True
    assert elapsed < launcher_cancel_deadline_seconds() + 0.75
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS

    release.set()
    time.sleep(0.05)


def test_revoke_queue_saturation_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_MAX_PENDING", "2")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "10")

    release = threading.Event()

    async def hang(*, job_id: str, **_kwargs: Any):
        from app.services.model_proxy_client import RevokeAttemptOutcome

        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)

    rs._reset_for_tests()

    assert rs.schedule_proxy_token_revoke("1" * 32) is True
    assert rs.schedule_proxy_token_revoke("2" * 32) is True
    # Third should be rejected when pending >= 2
    time.sleep(0.05)
    before = rs.pending_revoke_count()
    assert rs.schedule_proxy_token_revoke("3" * 32) is False
    # pending should not grow past max
    assert rs.pending_revoke_count() <= 2
    assert before <= 2
    release.set()
    time.sleep(0.1)
