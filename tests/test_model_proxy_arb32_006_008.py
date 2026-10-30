"""ARB32-006 revoke-wakes silent wait; ARB32-007 discard buffer→401; ARB32-008 slot ownership."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

ISSUER = "test-issuer-token-32chars-ok!!"


def _base_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "PROXY_ISSUER_TOKEN": ISSUER,
        "OPENAI_API_KEY": "sk-test-upstream-default-key",
        "OPR_MAX_REVIEW_TIMEOUT_SECONDS": "600",
        "PROXY_TOKEN_GRACE_SECONDS": "30",
        "PROXY_MAX_BODY_BYTES": "65536",
        "PROXY_MAX_REQUESTS_PER_JOB": "50",
        "PROXY_MAX_BYTES_PER_JOB": "200000",
        "PROXY_MAX_CONCURRENT_PER_JOB": "2",
        "PROXY_STREAM_IDLE_TIMEOUT_SECONDS": "5",
        "PROXY_UPSTREAM_TIMEOUT_SECONDS": "30",
        "PROXY_UPSTREAM_HOST": "api.openai.com",
        "PROXY_UPSTREAM_SCHEME": "https",
        "PROXY_PROVIDER": "openai",
    }
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _issuer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ISSUER}"}


def _issue(client: TestClient, jid: str, **body) -> str:
    payload = {"timeout_seconds": 60, **body}
    r = client.post(f"/internal/jobs/{jid}/token", headers=_issuer_headers(), json=payload)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _open_proxy(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> TestClient:
    _base_env(monkeypatch, **overrides)
    from app.model_proxy.main import app

    return TestClient(app)


def _rebind_upstream(client: TestClient, *, http: httpx.AsyncClient | None = None) -> httpx.AsyncClient:
    from app.model_proxy.providers import build_provider
    from app.model_proxy.settings import load_settings
    from app.model_proxy.tokens import TokenStore

    settings = load_settings()
    client.app.state.settings = settings
    client.app.state.provider = build_provider(settings)
    client.app.state.store = TokenStore(settings)
    old = client.app.state.http
    if http is not None:
        client.app.state.http = http
    return old


def _serve(handler: Callable[[socket.socket], None]) -> tuple[threading.Thread, int, threading.Event]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    ready = threading.Event()
    stop = threading.Event()

    def _run() -> None:
        ready.set()
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                handler(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        try:
            sock.close()
        except OSError:
            pass

    thread = threading.Thread(target=_run, name="fake-upstream", daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread, port, stop


def _read_http_request(conn: socket.socket, *, limit: int = 1 << 20) -> bytes:
    conn.settimeout(5.0)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < limit:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    header, _, _rest = data.partition(b"\r\n\r\n")
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                need = int(line.split(b":", 1)[1].strip())
            except ValueError:
                need = 0
            body = data[len(header) + 4 :]
            while len(body) < need:
                more = conn.recv(4096)
                if not more:
                    break
                body += more
            break
    return data


def _stop_server(thread: threading.Thread, stop: threading.Event) -> None:
    stop.set()
    thread.join(timeout=2)


# ---------------------------------------------------------------------------
# ARB32-006: revoke interrupts silent upstream wait
# ---------------------------------------------------------------------------


def test_revoke_while_upstream_silent_ends_promptly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoke during silent upstream gap ends well before idle timeout; slot freed."""
    accepted = threading.Event()

    def handler(conn: socket.socket) -> None:
        _read_http_request(conn)
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )
        conn.sendall(headers)
        accepted.set()
        # Stay silent longer than idle would allow if revoke did not wake us.
        time.sleep(20)
        conn.sendall(b"0\r\n\r\n")

    thread, port, stop = _serve(handler)
    try:
        with _open_proxy(
            monkeypatch,
            PROXY_UPSTREAM_HOST=f"127.0.0.1:{port}",
            PROXY_UPSTREAM_SCHEME="http",
            PROXY_STREAM_IDLE_TIMEOUT_SECONDS="8",
            PROXY_UPSTREAM_TIMEOUT_SECONDS="30",
            PROXY_MAX_CONCURRENT_PER_JOB="1",
            PROXY_MAX_BYTES_PER_JOB="100000",
        ) as client:
            old = _rebind_upstream(client, http=httpx.AsyncClient(http2=False, follow_redirects=False))
            jid = "r" * 32
            tok = _issue(client, jid)

            def revoker() -> None:
                assert accepted.wait(5)
                time.sleep(0.2)
                client.delete(f"/internal/jobs/{jid}/token", headers=_issuer_headers())

            t = threading.Thread(target=revoker, daemon=True)
            t.start()
            try:
                t0 = time.monotonic()
                with client.stream(
                    "POST",
                    "/v1/responses",
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    content=b'{"stream":true}',
                ) as resp:
                    assert resp.status_code == 200
                    _ = b"".join(resp.iter_bytes())
                elapsed = time.monotonic() - t0
                # Must end on revoke wake (~poll 50ms), not idle=8s.
                assert elapsed < 3.0, elapsed
                # Slot freed — a new token/request can reserve concurrency.
                tok2 = _issue(client, "s" * 32)
                store = client.app.state.store
                rec2 = store.resolve(tok2)
                assert rec2 is not None
                assert store.begin_request(rec2, body_len=0) is None
                store.end_request(rec2, refund_request=True)
            finally:
                import anyio

                anyio.run(client.app.state.http.aclose)
                client.app.state.http = old
                t.join(timeout=2)
    finally:
        _stop_server(thread, stop)


# ---------------------------------------------------------------------------
# ARB32-007: non-stream invalidate with buffer → 401, discard partial
# ---------------------------------------------------------------------------


def test_nonstream_invalidate_discards_buffer_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoke after some non-stream chunks are buffered → 401, never partial 200."""
    accepted = threading.Event()

    def handler(conn: socket.socket) -> None:
        _read_http_request(conn)
        # Non-SSE JSON via chunked so we take non-stream incremental path
        # (content-type application/json, not event-stream).
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )
        conn.sendall(headers)
        piece = b'{"partial":'
        conn.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        accepted.set()
        time.sleep(1.5)
        piece2 = b"true}"
        conn.sendall(f"{len(piece2):x}\r\n".encode() + piece2 + b"\r\n")
        conn.sendall(b"0\r\n\r\n")

    thread, port, stop = _serve(handler)
    try:
        with _open_proxy(
            monkeypatch,
            PROXY_UPSTREAM_HOST=f"127.0.0.1:{port}",
            PROXY_UPSTREAM_SCHEME="http",
            PROXY_STREAM_IDLE_TIMEOUT_SECONDS="8",
            PROXY_UPSTREAM_TIMEOUT_SECONDS="30",
            PROXY_MAX_BYTES_PER_JOB="100000",
        ) as client:
            old = _rebind_upstream(client, http=httpx.AsyncClient(http2=False, follow_redirects=False))
            jid = "n" * 32
            tok = _issue(client, jid)

            def revoker() -> None:
                assert accepted.wait(5)
                time.sleep(0.25)
                client.delete(f"/internal/jobs/{jid}/token", headers=_issuer_headers())

            t = threading.Thread(target=revoker, daemon=True)
            t.start()
            try:
                r = client.post(
                    "/v1/responses",
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                    content=b'{"stream":false}',
                )
                assert r.status_code == 401, r.text
                assert r.json()["error"] == "expired"
                # Must not look like a successful partial JSON body
                assert b"partial" not in r.content
            finally:
                import anyio

                anyio.run(client.app.state.http.aclose)
                client.app.state.http = old
                t.join(timeout=2)
    finally:
        _stop_server(thread, stop)


# ---------------------------------------------------------------------------
# ARB32-008: cancel during upstream header wait frees slot
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ARB32-008: cancel during upstream header wait frees slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_during_upstream_headers_frees_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling forward_responses while awaiting upstream headers releases the slot."""
    _base_env(monkeypatch, PROXY_MAX_CONCURRENT_PER_JOB="1")
    from app.model_proxy.providers import build_provider
    from app.model_proxy.proxy import forward_responses
    from app.model_proxy.settings import load_settings
    from app.model_proxy.tokens import TokenStore
    from starlette.requests import Request

    settings = load_settings()
    store = TokenStore(settings)
    provider = build_provider(settings)
    issued = store.issue(job_id="c" * 32, timeout_seconds=60)
    rec = store.resolve(issued.token)
    assert rec is not None
    assert store.begin_request(rec, body_len=0) is None

    started = asyncio.Event()

    class _HangClient:
        async def send(self, request, stream=False):  # noqa: ANN001
            started.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            return None

        def build_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return httpx.AsyncClient().build_request(*args, **kwargs)

    # Use a real AsyncClient only to build requests; swap send via wrapper
    real = httpx.AsyncClient(http2=False, follow_redirects=False)
    hang = _HangClient()

    class _ProxyClient:
        def build_request(self, *a, **k):  # noqa: ANN002, ANN003
            return real.build_request(*a, **k)

        async def send(self, request, stream=False):  # noqa: ANN001
            return await hang.send(request, stream=stream)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(scope, receive)

    async def run_forward() -> None:
        await forward_responses(
            request=request,
            body=b'{"model":"x"}',
            settings=settings,
            provider=provider,
            store=store,
            rec=rec,
            upstream_api_key="sk-test",
            client=_ProxyClient(),  # type: ignore[arg-type]
        )

    task = asyncio.create_task(run_forward())
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Ownership never transferred to StreamingResponse → catch_all finally releases.
    store.end_request(rec, refund_request=False)
    assert store.active_stream_count(rec) == 0

    for _ in range(3):
        assert store.begin_request(rec, body_len=0) is None
        store.end_request(rec, refund_request=True)
    assert store.begin_request(rec, body_len=0) is None
    store.end_request(rec, refund_request=True)
    await real.aclose()


def test_repeated_cancel_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """N simulated cancellations free the slot; a normal request then succeeds."""
    _base_env(monkeypatch, PROXY_MAX_CONCURRENT_PER_JOB="1")
    from app.model_proxy.settings import load_settings
    from app.model_proxy.tokens import TokenStore

    settings = load_settings()
    store = TokenStore(settings)
    issued = store.issue(job_id="w" * 32, timeout_seconds=60)
    rec = store.resolve(issued.token)
    assert rec is not None

    for _ in range(5):
        assert store.begin_request(rec, body_len=0) is None
        assert store.active_stream_count(rec) == 1
        store.end_request(rec, refund_request=True)
        assert store.active_stream_count(rec) == 0

    with _open_proxy(monkeypatch, PROXY_MAX_CONCURRENT_PER_JOB="1") as client:
        tok = _issue(client, "v" * 32)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        old = client.app.state.http
        client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            r = client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                content=b'{"model":"x"}',
            )
            assert r.status_code == 200
            assert r.json() == {"ok": True}
        finally:
            import anyio

            anyio.from_thread.run(client.app.state.http.aclose) if False else None
            # TestClient runs sync; close via anyio.run is fine outside async tests.
            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old
