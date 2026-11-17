"""Upstream failure classification, one terminal metric, and no secret leakage."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import ssl

import httpx
import pytest
from app.model_proxy import metrics
from app.model_proxy.failures import classify_transport, classify_upstream_status
from fastapi.testclient import TestClient
from tests.test_model_proxy_p1 import _issue, _open_proxy, _rebind_upstream


def _sample(metric, **labels) -> float:
    child = metric.labels(**labels) if labels else metric
    return float(child._value.get())


def _post(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
    jid: str,
    *,
    stream: bool = False,
) -> tuple[int, bytes]:
    with _open_proxy(monkeypatch) as client:
        assert isinstance(client, TestClient)
        old = _rebind_upstream(
            client,
            http=httpx.AsyncClient(transport=transport, http2=False, follow_redirects=False),
        )
        token = _issue(client, jid)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            if stream:
                headers["Accept"] = "text/event-stream"
                with client.stream(
                    "POST",
                    "/v1/responses",
                    headers=headers,
                    content=b'{"model":"x","stream":true}',
                ) as response:
                    return response.status_code, b"".join(response.iter_bytes())
            response = client.post(
                "/v1/responses",
                headers=headers,
                content=b'{"model":"x"}',
            )
            return response.status_code, response.content
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old


class _Raise(httpx.AsyncBaseTransport):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise self._exc


class _Status(httpx.AsyncBaseTransport):
    def __init__(self, status: int, body: bytes, *, content_type: str = "application/json") -> None:
        self._status = status
        self._body = body
        self._content_type = content_type

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self._status, content=self._body, headers={"content-type": self._content_type})


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: hi\n\n"
        raise httpx.RemoteProtocolError("peer closed")

    async def aclose(self) -> None:
        return None


class _Stream(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_BrokenStream(),
        )


def test_classify_timeout_and_transport_subtypes() -> None:
    cases = [
        (TimeoutError("overall"), "upstream_timeout", "overall_timeout", 504, "timeout"),
        (TimeoutError("idle"), "idle_timeout", "idle_timeout", 504, "idle_timeout"),
        (httpx.ConnectTimeout(""), "upstream_connect_timeout", "connect_timeout", 504, "connect_timeout"),
        (httpx.PoolTimeout(""), "upstream_pool_timeout", "pool_timeout", 504, "pool_timeout"),
        (httpx.ReadTimeout(""), "upstream_read_timeout", "read_timeout", 504, "read_timeout"),
        (httpx.WriteTimeout(""), "upstream_write_timeout", "write_timeout", 504, "write_timeout"),
        (httpx.TimeoutException(""), "upstream_timeout", "timeout", 504, "timeout"),
        (httpx.RemoteProtocolError(""), "upstream_reset", "connection_reset", 502, "connection_reset"),
        (httpx.ReadError(""), "upstream_reset", "connection_reset", 502, "connection_reset"),
        (httpx.WriteError(""), "upstream_reset", "connection_reset", 502, "connection_reset"),
        (ConnectionResetError(), "upstream_reset", "connection_reset", 502, "connection_reset"),
        (httpx.ProtocolError(""), "upstream_protocol", "protocol", 502, "protocol"),
        (httpx.HTTPError(""), "upstream_error", "unknown_transport", 502, "http_error"),
    ]
    for exc, error, kind, status, metric_kind in cases:
        assert classify_transport(exc) == (error, kind, status, metric_kind)

    dns = httpx.ConnectError("connect")
    dns.__cause__ = OSError("getaddrinfo failed: name or service not known")
    assert classify_transport(dns)[0] == "upstream_dns"

    tls = httpx.ConnectError("connect")
    tls.__cause__ = ssl.SSLError("certificate verify failed")
    assert classify_transport(tls)[0] == "upstream_tls"

    secret = httpx.ConnectError("https://example sk-supersecret bearer abc")
    error, kind, _status, metric_kind = classify_transport(secret)
    blob = f"{error} {kind} {metric_kind}"
    assert "sk-" not in blob
    assert "bearer" not in blob


def test_classify_status_keeps_auth_quota_and_rate_limit_apart() -> None:
    assert classify_upstream_status(200, b"{}") == ("ok", "ok")
    assert classify_upstream_status(401, b"nope") == ("upstream_auth", "upstream_auth")
    assert classify_upstream_status(500, b"nope") == ("upstream_5xx", "upstream_5xx")
    assert classify_upstream_status(429, b"slow down") == ("upstream_rate_limit", "rate_limit")
    quota = b'{"error":{"code":"insufficient_quota","message":"exceeded your current quota"}}'
    assert classify_upstream_status(429, quota) == ("upstream_quota", "quota")


def test_health_gauges_count_only_connectivity_failures() -> None:
    metrics.reset_model_health_for_tests()
    before_success = _sample(metrics.LAST_UPSTREAM_SUCCESS)
    metrics.note_model_attempt("upstream_connect_timeout", 0.1)
    metrics.note_model_attempt("upstream_5xx", 0.1)
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 2
    metrics.note_model_attempt("upstream_auth", 0.1)
    metrics.note_model_attempt("upstream_quota", 0.1)
    metrics.note_model_attempt("upstream_rate_limit", 0.1)
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 2
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == before_success
    metrics.note_model_attempt("ok", 0.2)
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) > 0


@pytest.mark.parametrize(
    ("exc", "error", "kind", "status"),
    [
        (httpx.ConnectTimeout("connect"), "upstream_connect_timeout", "connect_timeout", 504),
        (httpx.PoolTimeout("pool"), "upstream_pool_timeout", "pool_timeout", 504),
        (httpx.ReadTimeout("read"), "upstream_read_timeout", "read_timeout", 504),
        (httpx.WriteTimeout("write"), "upstream_write_timeout", "write_timeout", 504),
        (httpx.TimeoutException("overall"), "upstream_timeout", "timeout", 504),
    ],
)
def test_proxy_timeout_subtype_is_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException,
    error: str,
    kind: str,
    status: int,
) -> None:
    caplog.set_level(logging.INFO)
    before = _sample(metrics.REQUESTS, result=error)
    attempts = _sample(metrics.MODEL_ATTEMPTS, outcome="timeout")
    got_status, body = _post(monkeypatch, _Raise(exc), jid=("a" * 31) + error[-1])
    assert got_status == status
    payload = json.loads(body)
    assert payload["error"] == error
    assert payload["kind"] == kind
    assert _sample(metrics.REQUESTS, result=error) == before + 1
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="timeout") == attempts + 1
    assert "model_proxy outcome" in caplog.text
    assert error in caplog.text


def test_proxy_dns_tls_and_secret_are_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    dns = httpx.ConnectError("connect")
    dns.__cause__ = OSError("getaddrinfo failed sk-should-not-leak")
    status, body = _post(monkeypatch, _Raise(dns), jid="b" * 32)
    assert status == 502
    assert json.loads(body)["error"] == "upstream_dns"
    assert "sk-should-not-leak" not in caplog.text
    assert "sk-should-not-leak" not in body.decode()

    tls = httpx.ConnectError("handshake")
    tls.__cause__ = ssl.SSLError("certificate verify failed")
    status, body = _post(monkeypatch, _Raise(tls), jid="c" * 32)
    assert status == 502
    assert json.loads(body)["error"] == "upstream_tls"


def test_proxy_429_quota_and_rate_limit_pass_the_body_through(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    quota_body = b'{"error":{"code":"insufficient_quota","message":"exceeded your current quota"}}'
    before_quota = _sample(metrics.MODEL_ATTEMPTS, outcome="quota")
    status, body = _post(monkeypatch, _Status(429, quota_body), jid="d" * 32)
    assert status == 429
    assert body == quota_body
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="quota") == before_quota + 1
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0

    rate_body = b'{"error":{"code":"rate_limit_exceeded"}}'
    before_rate = _sample(metrics.MODEL_ATTEMPTS, outcome="rate_limit")
    status, body = _post(monkeypatch, _Status(429, rate_body), jid="e" * 32)
    assert status == 429
    assert body == rate_body
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="rate_limit") == before_rate + 1
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0


def test_proxy_401_is_not_a_successful_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    before = _sample(metrics.MODEL_ATTEMPTS, outcome="upstream_auth")
    status, body = _post(monkeypatch, _Status(401, b'{"error":"bad key"}'), jid="f" * 32)
    assert status == 401
    assert body == b'{"error":"bad key"}'
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="upstream_auth") == before + 1
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == 0
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0


def test_proxy_5xx_is_a_connectivity_failure_and_keeps_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    before = _sample(metrics.MODEL_ATTEMPTS, outcome="upstream_5xx")
    upstream = b'{"error":"overloaded"}'
    status, body = _post(monkeypatch, _Status(503, upstream), jid="1" * 32)
    assert status == 503
    assert body == upstream
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="upstream_5xx") == before + 1
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 1


def test_stream_break_after_headers_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    before_ok = _sample(metrics.REQUESTS, result="ok")
    before_fail = _sample(metrics.REQUESTS, result="stream_disconnect")
    before_attempts = _sample(metrics.MODEL_ATTEMPTS, outcome="stream_failed")
    status, body = _post(monkeypatch, _Stream(), jid="2" * 32, stream=True)
    assert status == 200
    assert b"data: hi" in body
    assert _sample(metrics.REQUESTS, result="ok") == before_ok
    assert _sample(metrics.REQUESTS, result="stream_disconnect") == before_fail + 1
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="stream_failed") == before_attempts + 1


def test_cancelled_request_is_not_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    with _open_proxy(monkeypatch) as client:
        old = _rebind_upstream(
            client,
            http=httpx.AsyncClient(transport=_Raise(asyncio.CancelledError()), http2=False, follow_redirects=False),
        )
        token = _issue(client, "3" * 32)
        before = _sample(metrics.REQUESTS, result="upstream_timeout")
        try:
            try:
                response = client.post(
                    "/v1/responses",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    content=b'{"model":"x"}',
                )
            except BaseException as exc:
                assert isinstance(exc, (asyncio.CancelledError, concurrent.futures.CancelledError))
                assert "upstream_" not in str(exc)
            else:
                pytest.fail(f"status={response.status_code} body={response.content!r}")
            assert _sample(metrics.REQUESTS, result="upstream_timeout") == before
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old
