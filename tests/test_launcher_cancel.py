"""Launcher DELETE /v1/jobs/{job_id} cancel endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.launcher.jobs import cancel_review_job
from app.launcher.paths import CheckoutPathError, validate_uuid_job_id
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def test_validate_uuid_job_id() -> None:
    assert len(validate_uuid_job_id("a" * 32)) == 32
    with pytest.raises(CheckoutPathError):
        validate_uuid_job_id("not-a-uuid")
    with pytest.raises(CheckoutPathError):
        validate_uuid_job_id("ai-review-" + "a" * 32)


def test_cancel_review_job_idempotent_when_empty() -> None:
    class FakeDocker:
        def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
            return []

        def close(self) -> None:
            return None

    result = cancel_review_job(job_id="b" * 32, client=FakeDocker())  # type: ignore[arg-type]
    assert result["job_id"] == "b" * 32
    assert result["removed"] == 0
    assert result["confirmed"] is True


def test_cancel_review_job_kills_and_removes() -> None:
    killed: list[str] = []
    removed: list[str] = []
    alive = [{"Id": "cidabcdef1234", "Labels": {"opr.job_id": "c" * 32}}]

    class FakeDocker:
        def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
            return list(alive)

        def kill_container(self, cid: str, *, timeout: float | None = None) -> None:
            killed.append(cid)

        def remove_container(self, cid: str, *, force: bool = True, timeout: float | None = None) -> None:
            removed.append(cid)
            alive.clear()

        def close(self) -> None:
            return None

    result = cancel_review_job(job_id="c" * 32, client=FakeDocker())  # type: ignore[arg-type]
    assert result["removed"] == 1
    assert result["confirmed"] is True
    assert killed == ["cidabcdef1234"]
    assert removed == ["cidabcdef1234"]


@pytest.fixture()
def cancel_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token = "test-launcher-token-32chars-ok"
    monkeypatch.setenv("LAUNCHER_TOKEN", token)
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))

    from app.launcher.auth import require_launcher_auth
    from app.launcher.main import cancel_job, healthz

    app = FastAPI()
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route(
        "/v1/jobs/{job_id}",
        cancel_job,
        methods=["DELETE"],
        dependencies=[Depends(require_launcher_auth)],
    )
    return TestClient(app), token


def test_cancel_endpoint_requires_auth(cancel_client) -> None:
    client, _token = cancel_client
    r = client.delete(f"/v1/jobs/{'d' * 32}")
    assert r.status_code == 401


def test_cancel_endpoint_rejects_non_uuid(cancel_client) -> None:
    client, token = cancel_client
    r = client.delete("/v1/jobs/not-a-uuid", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400


def test_cancel_endpoint_idempotent(cancel_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, token = cancel_client
    monkeypatch.setattr(
        "app.launcher.main.cancel_review_job",
        lambda **kwargs: {"job_id": kwargs["job_id"], "removed": 0, "confirmed": True},
    )
    r = client.delete(
        f"/v1/jobs/{'e' * 32}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == {"job_id": "e" * 32, "removed": 0, "confirmed": True}
    r2 = client.delete(
        f"/v1/jobs/{'e' * 32}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["removed"] == 0
    assert r2.json()["confirmed"] is True
