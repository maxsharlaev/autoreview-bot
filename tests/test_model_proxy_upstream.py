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
from app.model_proxy.proxy import _SseTerminals
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
    **env: str,
) -> tuple[int, bytes]:
    with _open_proxy(monkeypatch, **env) as client:
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
    assert classify_upstream_status(204, b"") == ("ok", "ok")
    assert classify_upstream_status(307, b"") == ("upstream_redirect", "client_error")
    assert classify_upstream_status(302, b"") == ("upstream_redirect", "client_error")
    assert classify_upstream_status(401, b"nope") == ("upstream_auth", "upstream_auth")
    assert classify_upstream_status(500, b"nope") == ("upstream_5xx", "upstream_5xx")
    assert classify_upstream_status(400, b"bad request") == ("upstream_status", "client_error")
    assert classify_upstream_status(404, b"missing") == ("upstream_status", "client_error")
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
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == before_success
    metrics.note_model_attempt("upstream_timeout", 0.1)
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 1
    for result in (
        "upstream_quota",
        "upstream_rate_limit",
        "upstream_status",
        "upstream_redirect",
        "response_incomplete",
        "response_failed",
    ):
        metrics.note_model_attempt(result, 0.1)
        assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
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


def test_proxy_4xx_counts_as_an_attempt_but_not_a_connectivity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    metrics.note_model_attempt("upstream_connect_timeout", 0.01)
    last_success = _sample(metrics.LAST_UPSTREAM_SUCCESS)
    before = _sample(metrics.MODEL_ATTEMPTS, outcome="client_error")
    upstream = b'{"error":"invalid_request"}'
    status, body = _post(monkeypatch, _Status(400, upstream), jid="6" * 32)
    assert status == 400
    assert body == upstream
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="client_error") == before + 1
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == last_success


def test_proxy_5xx_is_a_connectivity_failure_with_a_stable_token(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    before = _sample(metrics.MODEL_ATTEMPTS, outcome="upstream_5xx")
    status, body = _post(monkeypatch, _Status(503, b'{"error":"overloaded"}'), jid="1" * 32)
    assert status == 503
    payload = json.loads(body)
    assert payload["error"] == "upstream_5xx"
    assert payload["kind"] == "upstream_5xx"
    assert b"overloaded" not in body
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="upstream_5xx") == before + 1
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 1


def test_stream_break_after_headers_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    before_ok = _sample(metrics.REQUESTS, result="ok")
    before_reset = _sample(metrics.REQUESTS, result="upstream_reset")
    before_attempts = _sample(metrics.MODEL_ATTEMPTS, outcome="transport")
    status, body = _post(monkeypatch, _Stream(), jid="2" * 32, stream=True)
    assert status == 200
    assert b"data: hi" in body
    assert _sample(metrics.REQUESTS, result="ok") == before_ok
    assert _sample(metrics.REQUESTS, result="upstream_reset") == before_reset + 1
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="transport") == before_attempts + 1


class _ReadTimeoutStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: hi\n\n"
        raise httpx.ReadTimeout("read timed out")

    async def aclose(self) -> None:
        return None


class _ReadTimeoutTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ReadTimeoutStream(),
        )


def test_stream_read_timeout_after_headers_is_not_a_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    before_timeout = _sample(metrics.REQUESTS, result="upstream_read_timeout")
    before_disconnect = _sample(metrics.REQUESTS, result="stream_disconnect")
    before_attempts = _sample(metrics.MODEL_ATTEMPTS, outcome="timeout")
    before_errors = _sample(metrics.UPSTREAM_ERRORS, kind="read_timeout")
    status, body = _post(monkeypatch, _ReadTimeoutTransport(), jid="7" * 32, stream=True)
    assert status == 200
    assert b"data: hi" in body
    assert _sample(metrics.REQUESTS, result="upstream_read_timeout") == before_timeout + 1
    assert _sample(metrics.REQUESTS, result="stream_disconnect") == before_disconnect
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="timeout") == before_attempts + 1
    assert _sample(metrics.UPSTREAM_ERRORS, kind="read_timeout") == before_errors + 1
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == 0


_PARTIAL_SSE = (
    b"event: response.created\n"
    b'data: {"type":"response.created"}\n\n'
    b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
)
_COMPLETE_SSE = _PARTIAL_SSE + b'event: response.completed\ndata: {"type":"response.completed"}\n\n'


def test_incomplete_sse_eof_is_not_a_successful_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    before_ok = _sample(metrics.REQUESTS, result="ok")
    before_disconnect = _sample(metrics.REQUESTS, result="stream_disconnect")
    before_attempts = _sample(metrics.MODEL_ATTEMPTS, outcome="stream_failed")
    status, body = _post(
        monkeypatch,
        _Status(200, _PARTIAL_SSE, content_type="text/event-stream"),
        jid="8" * 32,
        stream=True,
    )
    assert status == 200
    assert b"response.output_text.delta" in body
    assert b"response.completed" not in body
    assert _sample(metrics.REQUESTS, result="ok") == before_ok
    assert _sample(metrics.REQUESTS, result="stream_disconnect") == before_disconnect + 1
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="stream_failed") == before_attempts + 1
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == 0
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 1


def test_completed_sse_is_a_successful_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    metrics.note_model_attempt("upstream_connect_timeout", 0.01)
    before_ok = _sample(metrics.REQUESTS, result="ok")
    before_disconnect = _sample(metrics.REQUESTS, result="stream_disconnect")
    status, body = _post(
        monkeypatch,
        _Status(200, _COMPLETE_SSE, content_type="text/event-stream"),
        jid="9" * 32,
        stream=True,
    )
    assert status == 200
    assert b"response.completed" in body
    assert _sample(metrics.REQUESTS, result="ok") == before_ok + 1
    assert _sample(metrics.REQUESTS, result="stream_disconnect") == before_disconnect
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) > 0


@pytest.mark.parametrize(
    ("event", "result", "outcome"),
    [
        ("response.incomplete", "response_incomplete", "incomplete"),
        ("response.failed", "response_failed", "response_failed"),
    ],
)
def test_terminal_sse_failure_is_not_a_connectivity_failure(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    result: str,
    outcome: str,
) -> None:
    metrics.reset_model_health_for_tests()
    metrics.note_model_attempt("upstream_connect_timeout", 0.01)
    last_success = _sample(metrics.LAST_UPSTREAM_SUCCESS)
    before_result = _sample(metrics.REQUESTS, result=result)
    before_disconnect = _sample(metrics.REQUESTS, result="stream_disconnect")
    before_outcome = _sample(metrics.MODEL_ATTEMPTS, outcome=outcome)
    payload = _PARTIAL_SSE + f'event: {event}\ndata: {{"type":"{event}"}}\n\n'.encode()
    status, body = _post(
        monkeypatch,
        _Status(200, payload, content_type="text/event-stream"),
        jid=("c" if event.endswith("incomplete") else "d") * 32,
        stream=True,
    )
    assert status == 200
    assert event.encode() in body
    assert _sample(metrics.REQUESTS, result=result) == before_result + 1
    assert _sample(metrics.REQUESTS, result="stream_disconnect") == before_disconnect
    assert _sample(metrics.MODEL_ATTEMPTS, outcome=outcome) == before_outcome + 1
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == last_success


def test_redirect_is_not_a_successful_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    metrics.note_model_attempt("upstream_connect_timeout", 0.01)
    last_success = _sample(metrics.LAST_UPSTREAM_SUCCESS)
    before = _sample(metrics.MODEL_ATTEMPTS, outcome="client_error")
    before_redirect = _sample(metrics.REQUESTS, result="upstream_redirect")
    status, body = _post(monkeypatch, _Status(307, b"go elsewhere"), jid="e" * 32)
    assert status == 307
    assert body == b"go elsewhere"
    assert _sample(metrics.REQUESTS, result="upstream_redirect") == before_redirect + 1
    assert _sample(metrics.MODEL_ATTEMPTS, outcome="client_error") == before + 1
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == last_success


class _AfterTerminal(httpx.AsyncByteStream):
    def __init__(self, body: bytes, mode: str) -> None:
        self._body = body
        self._mode = mode
        self._release = asyncio.Event()

    async def __aiter__(self):
        yield self._body
        if self._mode == "reset":
            raise httpx.RemoteProtocolError("closed after terminal")
        try:
            await asyncio.wait_for(self._release.wait(), timeout=2)
        except TimeoutError:
            return

    async def aclose(self) -> None:
        self._release.set()


class _AfterTerminalTransport(httpx.AsyncBaseTransport):
    def __init__(self, body: bytes, mode: str) -> None:
        self._body = body
        self._mode = mode

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_AfterTerminal(self._body, self._mode),
        )


@pytest.mark.parametrize(
    ("event", "result", "outcome", "success"),
    [
        ("response.completed", "ok", "ok", True),
        ("response.incomplete", "response_incomplete", "incomplete", False),
        ("response.failed", "response_failed", "response_failed", False),
    ],
)
@pytest.mark.parametrize("mode", ["idle", "reset"])
def test_terminal_sse_survives_a_late_disconnect(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    result: str,
    outcome: str,
    success: bool,
    mode: str,
) -> None:
    metrics.reset_model_health_for_tests()
    metrics.note_model_attempt("upstream_connect_timeout", 0.01)
    last_success = _sample(metrics.LAST_UPSTREAM_SUCCESS)
    before_result = _sample(metrics.REQUESTS, result=result)
    before_idle = _sample(metrics.REQUESTS, result="idle_timeout")
    before_reset = _sample(metrics.REQUESTS, result="upstream_reset")
    payload = _PARTIAL_SSE + f'event: {event}\ndata: {{"type":"{event}"}}\n\n'.encode()
    tag = {"response.completed": "1", "response.incomplete": "2", "response.failed": "3"}[event]
    status, body = _post(
        monkeypatch,
        _AfterTerminalTransport(payload, mode),
        jid=(tag + mode[0]) * 16,
        stream=True,
        PROXY_STREAM_IDLE_TIMEOUT_SECONDS="0.4",
    )
    assert status == 200
    assert event.encode() in body
    assert _sample(metrics.REQUESTS, result=result) == before_result + 1
    assert _sample(metrics.REQUESTS, result="idle_timeout") == before_idle
    assert _sample(metrics.REQUESTS, result="upstream_reset") == before_reset
    if success:
        assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
        assert _sample(metrics.LAST_UPSTREAM_SUCCESS) > 0
    else:
        assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == 0
        assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == last_success
        assert _sample(metrics.MODEL_ATTEMPTS, outcome=outcome) >= 1


class _ChunksThenIdle(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self._release = asyncio.Event()

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        try:
            await asyncio.wait_for(self._release.wait(), timeout=2)
        except TimeoutError:
            return

    async def aclose(self) -> None:
        self._release.set()


class _ChunksThenIdleTransport(httpx.AsyncBaseTransport):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ChunksThenIdle(self._chunks),
        )


def _terminal_frame(event: str) -> bytes:
    return f'event: {event}\ndata: {{"type":"{event}"}}\n\n'.encode()


def _split_terminal(event: str, split: str) -> list[bytes]:
    frame = _terminal_frame(event)
    if split == "after_event":
        at = len(f"event: {event}\n".encode())
    else:
        needle = f'"type":"{event}"'.encode()
        at = frame.index(needle) + len(needle) - 4
    return [frame[:at], frame[at:]]


def _decoy_frame() -> bytes:
    payload = json.dumps(
        {
            "type": "response.output_text.delta",
            "delta": 'event: response.completed "type":"response.completed"',
        }
    ).encode()
    return b"event: response.output_text.delta\ndata: " + payload + b"\n\n"


def test_sse_terminal_waits_for_a_complete_frame() -> None:
    seen = _SseTerminals()
    seen.feed(b"event: response.completed\n")
    assert seen.terminal == ""
    seen.feed(b'data: {"type":"response.com')
    assert seen.terminal == ""
    seen.feed(b'pleted"}\n\n')
    assert seen.terminal == "response.completed"

    decoy = _SseTerminals()
    decoy.feed(_decoy_frame())
    assert decoy.terminal == ""

    ranked = _SseTerminals()
    ranked.feed(_terminal_frame("response.incomplete") + _terminal_frame("response.completed"))
    assert ranked.terminal == "response.completed"


@pytest.mark.parametrize(
    ("event", "result", "outcome", "success"),
    [
        ("response.completed", "ok", "ok", True),
        ("response.incomplete", "response_incomplete", "incomplete", False),
        ("response.failed", "response_failed", "response_failed", False),
    ],
)
@pytest.mark.parametrize("split", ["after_event", "inside_json"])
def test_terminal_sse_split_across_chunks_is_forwarded_whole(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    result: str,
    outcome: str,
    success: bool,
    split: str,
) -> None:
    metrics.reset_model_health_for_tests()
    before_result = _sample(metrics.REQUESTS, result=result)
    before_idle = _sample(metrics.REQUESTS, result="idle_timeout")
    chunks = _split_terminal(event, split)
    tag = {"response.completed": "1", "response.incomplete": "2", "response.failed": "3"}[event]
    mark = "a" if split == "after_event" else "j"
    status, body = _post(
        monkeypatch,
        _ChunksThenIdleTransport(chunks),
        jid=(tag + mark) * 16,
        stream=True,
        PROXY_STREAM_IDLE_TIMEOUT_SECONDS="0.4",
    )
    assert status == 200
    assert body == b"".join(chunks)
    assert _sample(metrics.REQUESTS, result=result) == before_result + 1
    assert _sample(metrics.REQUESTS, result="idle_timeout") == before_idle
    if success:
        assert _sample(metrics.LAST_UPSTREAM_SUCCESS) > 0
    else:
        assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == 0
        assert _sample(metrics.MODEL_ATTEMPTS, outcome=outcome) >= 1


def test_marker_text_inside_a_delta_is_not_a_terminal_event(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    before_ok = _sample(metrics.REQUESTS, result="ok")
    before_disconnect = _sample(metrics.REQUESTS, result="stream_disconnect")
    payload = _decoy_frame()
    status, body = _post(
        monkeypatch,
        _Status(200, payload, content_type="text/event-stream"),
        jid="m" * 32,
        stream=True,
    )
    assert status == 200
    assert body == payload
    assert _sample(metrics.REQUESTS, result="ok") == before_ok
    assert _sample(metrics.REQUESTS, result="stream_disconnect") == before_disconnect + 1
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == 0


def test_marker_text_does_not_end_the_stream_before_the_real_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics.reset_model_health_for_tests()
    before_ok = _sample(metrics.REQUESTS, result="ok")
    chunks = [_decoy_frame(), _terminal_frame("response.completed")]
    status, body = _post(
        monkeypatch,
        _ChunksThenIdleTransport(chunks),
        jid="n" * 32,
        stream=True,
        PROXY_STREAM_IDLE_TIMEOUT_SECONDS="0.4",
    )
    assert status == 200
    assert body == b"".join(chunks)
    assert _sample(metrics.REQUESTS, result="ok") == before_ok + 1
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) > 0


def test_stream_cancel_after_headers_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.reset_model_health_for_tests()
    metrics.note_model_attempt("upstream_connect_timeout", 0.01)
    streak = _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES)
    last_success = _sample(metrics.LAST_UPSTREAM_SUCCESS)
    before_ok = _sample(metrics.REQUESTS, result="ok")
    calls = {"n": 0}
    from app.model_proxy import proxy as proxy_mod

    real = proxy_mod._anext_or_invalidated

    async def cancel_on_next_chunk(aiter, rec, wait):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError()
        return await real(aiter, rec, wait)

    monkeypatch.setattr(proxy_mod, "_anext_or_invalidated", cancel_on_next_chunk)

    class _QuietStream(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: hi\n\ndata: more\n\n",
            )

    with _open_proxy(monkeypatch) as client:
        old = _rebind_upstream(
            client,
            http=httpx.AsyncClient(transport=_QuietStream(), http2=False, follow_redirects=False),
        )
        token = _issue(client, "4" * 32)
        try:
            with pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)):
                with client.stream(
                    "POST",
                    "/v1/responses",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    content=b'{"model":"x","stream":true}',
                ) as response:
                    assert response.status_code == 200
                    b"".join(response.iter_bytes())
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old
    assert calls["n"] >= 2
    assert _sample(metrics.REQUESTS, result="ok") == before_ok
    assert _sample(metrics.CONSECUTIVE_UPSTREAM_FAILURES) == streak
    assert _sample(metrics.LAST_UPSTREAM_SUCCESS) == last_success


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
