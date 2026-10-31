"""ARB34-007: launcher 426 upgrade-required; worker surfaces clearly; never forward key."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from app.services.codex_runner import CodexRunnerError
from app.services.constants import CODEX_UPGRADE_REQUIRED
from app.services.container_executor import ContainerReviewExecutor
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def review_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token = "test-launcher-token-32chars-ok"
    monkeypatch.setenv("LAUNCHER_TOKEN", token)
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_REVIEW_ORPHAN_TTL_SECONDS", "7200")
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")

    from app.launcher.auth import require_launcher_auth
    from app.launcher.main import start_review

    app = FastAPI()
    app.add_api_route(
        "/v1/reviews",
        start_review,
        methods=["POST"],
        dependencies=[Depends(require_launcher_auth)],
    )
    return TestClient(app), token, tmp_path


def test_missing_proxy_token_returns_426(review_app) -> None:
    client, token, base = review_app
    (base / "abc" / "repo").mkdir(parents=True)
    r = client.post(
        "/v1/reviews",
        headers={"Authorization": f"Bearer {token}"},
        json={"job_id": "abc", "checkout_ref": "abc/repo"},
    )
    assert r.status_code == 426
    detail = r.json()["detail"]
    assert detail["code"] == CODEX_UPGRADE_REQUIRED
    assert "upgrade worker, launcher and model-proxy together" in detail["message"]
    assert detail["reason"] == "missing_proxy_token"


def test_legacy_model_key_header_returns_426_never_forwards(
    review_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, token, base = review_app
    (base / "abc" / "repo").mkdir(parents=True)
    seen: dict[str, Any] = {}

    def boom(**kwargs):
        seen.update(kwargs)
        raise AssertionError("run_review_job must not be called when legacy header present")

    monkeypatch.setattr("app.launcher.main.run_review_job", boom)
    r = client.post(
        "/v1/reviews",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Model-Api-Key": "sk-must-never-be-forwarded",
        },
        json={
            "job_id": "abc",
            "checkout_ref": "abc/repo",
            "proxy_token": "opr_job_test_token_ok",
        },
    )
    assert r.status_code == 426
    detail = r.json()["detail"]
    assert detail["code"] == CODEX_UPGRADE_REQUIRED
    assert detail["reason"] == "legacy_x_model_api_key_header"
    assert "sk-must-never" not in r.text
    assert seen == {}


@pytest.mark.asyncio
async def test_worker_surfaces_426_as_upgrade_required(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = ContainerReviewExecutor(launcher_url="http://launcher", launcher_token="t" * 32)

    class FakeResp:
        status_code = 426

        def json(self):
            return {
                "detail": {
                    "code": CODEX_UPGRADE_REQUIRED,
                    "message": "worker too old: upgrade worker, launcher and model-proxy together",
                }
            }

        @property
        def text(self) -> str:
            return "no-secret"

    async def fake_post(*_a, **_k):
        return FakeResp()

    client = httpx.AsyncClient()
    monkeypatch.setattr(client, "post", fake_post)
    ex._http_client = client
    with pytest.raises(CodexRunnerError) as ei:
        await ex._call_launcher(
            job_id="a" * 32,
            checkout_ref="a" * 32 + "/repo",
            proxy_token="opr_job_x",
            timeout_seconds=30,
        )
    err = ei.value
    assert err.code == CODEX_UPGRADE_REQUIRED
    assert "upgrade worker, launcher and model-proxy together" in str(err)
    await client.aclose()


@pytest.mark.asyncio
async def test_worker_maps_bare_422_to_upgrade_required(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = ContainerReviewExecutor(launcher_url="http://launcher", launcher_token="t" * 32)

    class FakeResp:
        status_code = 422
        text = '{"detail":[{"type":"missing","loc":["body","proxy_token"]}]}'

        def json(self):
            return {"detail": [{"type": "missing", "loc": ["body", "proxy_token"]}]}

    async def fake_post(*_a, **_k):
        return FakeResp()

    client = httpx.AsyncClient()
    monkeypatch.setattr(client, "post", fake_post)
    ex._http_client = client
    with pytest.raises(CodexRunnerError) as ei:
        await ex._call_launcher(
            job_id="b" * 32,
            checkout_ref="b" * 32 + "/repo",
            proxy_token="opr_job_x",
            timeout_seconds=30,
        )
    assert ei.value.code == CODEX_UPGRADE_REQUIRED
    await client.aclose()
