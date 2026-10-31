"""Launcher HTTP API: auth required, body limited, fail-closed mapping."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def launcher_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token = "test-launcher-token-32chars-ok"
    monkeypatch.setenv("LAUNCHER_TOKEN", token)
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    return token, tmp_path


@pytest.fixture()
def client(launcher_env, monkeypatch: pytest.MonkeyPatch):
    # Avoid starting the real reaper loop against Docker during unit tests.
    async def _noop_lifespan(app):
        yield

    from app.launcher import main as launcher_main

    monkeypatch.setattr(launcher_main, "lifespan", _noop_lifespan)
    # Rebuild app with patched lifespan
    from app.launcher.auth import require_launcher_auth
    from app.launcher.main import healthz, start_review
    from fastapi import Depends, FastAPI

    app = FastAPI()
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route(
        "/v1/reviews",
        start_review,
        methods=["POST"],
        dependencies=[Depends(require_launcher_auth)],
    )
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


def test_reviews_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/v1/reviews",
        json={"job_id": "abc", "checkout_ref": "abc/repo", "proxy_token": "opr_job_test_token_ok"},
    )
    assert r.status_code == 401


def test_reviews_rejects_bad_token(client: TestClient, launcher_env) -> None:
    token, _ = launcher_env
    r = client.post(
        "/v1/reviews",
        headers={"Authorization": "Bearer wrong-token-wrong-token", "X-Model-Api-Key": "sk"},
        json={"job_id": "abc", "checkout_ref": "abc/repo", "proxy_token": "opr_job_test_token_ok"},
    )
    assert r.status_code == 401


def test_reviews_requires_proxy_token(client: TestClient, launcher_env) -> None:
    token, base = launcher_env
    job = base / "abc" / "repo"
    job.mkdir(parents=True)
    (base / "abc" / "prompt.txt").write_text("p", encoding="utf-8")
    (base / "abc" / "schema.json").write_text("{}", encoding="utf-8")
    r = client.post(
        "/v1/reviews",
        headers={"Authorization": f"Bearer {token}"},
        json={"job_id": "abc", "checkout_ref": "abc/repo"},
    )
    assert r.status_code == 426  # ARB34-007: clear upgrade-required, not bare 422
    assert r.json()["detail"]["code"] == "CODEX_UPGRADE_REQUIRED"


def test_reviews_fail_closed_on_docker_error(client: TestClient, launcher_env, monkeypatch: pytest.MonkeyPatch) -> None:
    token, base = launcher_env
    (base / "abc" / "repo").mkdir(parents=True)
    (base / "abc" / "prompt.txt").write_text("p", encoding="utf-8")
    (base / "abc" / "schema.json").write_text("{}", encoding="utf-8")
    (base / "abc" / "job.json").write_text('{"model":"gpt-test","timeout_seconds":30}', encoding="utf-8")

    def boom(**kwargs):
        from app.launcher.jobs import LauncherJobError

        raise LauncherJobError("CODEX_SANDBOX", "docker error: simulated")

    monkeypatch.setattr("app.launcher.main.run_review_job", boom)
    r = client.post(
        "/v1/reviews",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "job_id": "abc",
            "checkout_ref": "abc/repo",
            "proxy_token": "opr_job_test_token_ok",
        },
    )
    assert r.status_code == 502
    body = r.json()
    assert body["detail"]["code"] == "CODEX_SANDBOX"
