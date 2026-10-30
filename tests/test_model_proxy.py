"""Tests for M3 model-proxy service (tokens, allowlist, limits, streaming)."""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "PROXY_ISSUER_TOKEN": "test-issuer-token-32chars-ok!!",
        "OPENAI_API_KEY": "sk-test-upstream-default-key",
        "PROXY_OWNER_ACME_OPENAI_API_KEY": "sk-test-upstream-acme-key",
        "OPR_MAX_REVIEW_TIMEOUT_SECONDS": "600",
        "PROXY_TOKEN_GRACE_SECONDS": "30",
        "PROXY_MAX_BODY_BYTES": "4096",
        "PROXY_MAX_REQUESTS_PER_JOB": "3",
        "PROXY_MAX_BYTES_PER_JOB": "20000",
        "PROXY_MAX_CONCURRENT_PER_JOB": "2",
        "PROXY_STREAM_IDLE_TIMEOUT_SECONDS": "5",
        "PROXY_UPSTREAM_TIMEOUT_SECONDS": "5",
        "PROXY_UPSTREAM_HOST": "api.openai.com",
        "PROXY_PROVIDER": "openai",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


@pytest.fixture()
def proxy_client(proxy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    # Import after env is set so lifespan validate_settings sees it.
    from app.model_proxy.main import app

    with TestClient(app) as client:
        yield client


def _issuer_headers(token: str = "test-issuer-token-32chars-ok!!") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_healthz(proxy_client: TestClient) -> None:
    r = proxy_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_issue_and_revoke_token(proxy_client: TestClient) -> None:
    jid = "a" * 32
    r = proxy_client.post(
        f"/internal/jobs/{jid}/token",
        headers=_issuer_headers(),
        json={"timeout_seconds": 120, "owner_id": "ACME"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == jid
    assert body["token"].startswith("opr_job_")
    assert body["expires_in"] == 150  # 120 + 30 grace
    assert body["owner_id"] == "ACME"

    rev = proxy_client.delete(f"/internal/jobs/{jid}/token", headers=_issuer_headers())
    assert rev.status_code == 200
    assert rev.json()["revoked"] is True

    # Second revoke is idempotent miss
    rev2 = proxy_client.delete(f"/internal/jobs/{jid}/token", headers=_issuer_headers())
    assert rev2.json()["revoked"] is False


def test_issue_requires_issuer_auth(proxy_client: TestClient) -> None:
    r = proxy_client.post(
        f"/internal/jobs/{'b' * 32}/token",
        json={"timeout_seconds": 10},
    )
    assert r.status_code == 401


def test_issue_rejects_timeout_above_max(proxy_client: TestClient) -> None:
    r = proxy_client.post(
        f"/internal/jobs/{'c' * 32}/token",
        headers=_issuer_headers(),
        json={"timeout_seconds": 601},
    )
    assert r.status_code == 400


def test_token_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "test-issuer-token-32chars-ok!!")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-upstream-default-key")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "0")
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    from app.model_proxy.main import app

    with TestClient(app) as client:
        jid = "d" * 32
        r = client.post(
            f"/internal/jobs/{jid}/token",
            headers=_issuer_headers(),
            json={"timeout_seconds": 1},
        )
        assert r.status_code == 200
        assert r.json()["expires_in"] == 1
        token = r.json()["token"]
        time.sleep(1.2)
        fr = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            content=b'{"model":"x","stream":false}',
        )
        assert fr.status_code == 401


def test_allowlist_rejects_get_and_websocket(proxy_client: TestClient) -> None:
    jid = "e" * 32
    tok = proxy_client.post(
        f"/internal/jobs/{jid}/token",
        headers=_issuer_headers(),
        json={"timeout_seconds": 60},
    ).json()["token"]
    r = proxy_client.get("/v1/responses", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    r2 = proxy_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        content=b"{}",
    )
    assert r2.status_code == 403
    r3 = proxy_client.post(
        "/v1/responses",
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
        },
        content=b'{"model":"x"}',
    )
    assert r3.status_code == 403


def test_body_size_limit(proxy_client: TestClient) -> None:
    jid = "f" * 32
    tok = proxy_client.post(
        f"/internal/jobs/{jid}/token",
        headers=_issuer_headers(),
        json={"timeout_seconds": 60},
    ).json()["token"]
    big = b"x" * 5000
    r = proxy_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        content=big,
    )
    assert r.status_code == 413


def test_streaming_passthrough(proxy_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock upstream SSE and assert bytes are streamed through."""
    import app.model_proxy.main as main_mod

    jid = "g" * 32
    with TestClient(main_mod.app) as client:
        tok = client.post(
            f"/internal/jobs/{jid}/token",
            headers=_issuer_headers(),
            json={"timeout_seconds": 60, "owner_id": "ACME"},
        ).json()["token"]

        sse = (
            b'event: response.created\ndata: {"type":"response.created"}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert str(request.url) == "https://api.openai.com/v1/responses"
            assert request.headers.get("Authorization") == "Bearer sk-test-upstream-acme-key"
            # Client job token must not be forwarded.
            assert "opr_job_" not in (request.headers.get("Authorization") or "")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse,
            )

        transport = httpx.MockTransport(handler)
        # Replace app http client
        old = client.app.state.http
        client.app.state.http = httpx.AsyncClient(transport=transport)
        try:
            with client.stream(
                "POST",
                "/v1/responses",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                content=b'{"model":"gpt-4.1-mini","stream":true,"input":[]}',
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")
                data = b"".join(resp.iter_bytes())
            assert b"response.output_text.delta" in data
            assert b"hi" in data
        finally:
            # AsyncClient close
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old


def test_revoked_token_cannot_call(proxy_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    jid = "h" * 32
    tok = proxy_client.post(
        f"/internal/jobs/{jid}/token",
        headers=_issuer_headers(),
        json={"timeout_seconds": 60},
    ).json()["token"]
    proxy_client.delete(f"/internal/jobs/{jid}/token", headers=_issuer_headers())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called")

    transport = httpx.MockTransport(handler)
    old = proxy_client.app.state.http
    proxy_client.app.state.http = httpx.AsyncClient(transport=transport)
    try:
        r = proxy_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            content=b'{"model":"x"}',
        )
        assert r.status_code == 401
    finally:
        import anyio

        anyio.run(proxy_client.app.state.http.aclose)
        proxy_client.app.state.http = old


def test_logs_omit_secrets_and_bodies(proxy_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    jid = "i" * 32
    with caplog.at_level(logging.INFO, logger="app.model_proxy.tokens"):
        r = proxy_client.post(
            f"/internal/jobs/{jid}/token",
            headers=_issuer_headers(),
            json={"timeout_seconds": 60},
        )
    token = r.json()["token"]
    joined = " ".join(rec.message for rec in caplog.records)
    assert "sk-test" not in joined
    assert token not in joined
    assert "opr_job_" not in joined


def test_request_limit(proxy_client: TestClient) -> None:
    jid = "j" * 32
    tok = proxy_client.post(
        f"/internal/jobs/{jid}/token",
        headers=_issuer_headers(),
        json={"timeout_seconds": 60},
    ).json()["token"]

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"id": "ok", "status": "completed"})

    transport = httpx.MockTransport(handler)
    old = proxy_client.app.state.http
    proxy_client.app.state.http = httpx.AsyncClient(transport=transport)
    try:
        for _ in range(3):
            r = proxy_client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                content=b'{"model":"x"}',
            )
            assert r.status_code == 200
        r4 = proxy_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            content=b'{"model":"x"}',
        )
        assert r4.status_code == 429
        assert calls["n"] == 3
    finally:
        import anyio

        anyio.run(proxy_client.app.state.http.aclose)
        proxy_client.app.state.http = old


def test_metrics_endpoint(proxy_client: TestClient) -> None:
    r = proxy_client.get("/metrics")
    assert r.status_code == 200
    assert "model_proxy" in r.text or r.headers["content-type"].startswith("text/plain")


def test_startup_fails_without_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_ISSUER_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    from app.model_proxy.settings import validate_settings

    with pytest.raises(RuntimeError, match="PROXY_ISSUER_TOKEN"):
        validate_settings()
