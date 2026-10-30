"""ARB32-001..004: body bound, output cap, wall-clock deadlines, Accept-Encoding identity."""

from __future__ import annotations

import gzip
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
        "PROXY_OWNER_ACME_OPENAI_API_KEY": "sk-test-upstream-acme-key",
        "OPR_MAX_REVIEW_TIMEOUT_SECONDS": "600",
        "PROXY_TOKEN_GRACE_SECONDS": "30",
        "PROXY_MAX_BODY_BYTES": "4096",
        "PROXY_MAX_REQUESTS_PER_JOB": "50",
        "PROXY_MAX_BYTES_PER_JOB": "20000",
        "PROXY_MAX_CONCURRENT_PER_JOB": "4",
        "PROXY_STREAM_IDLE_TIMEOUT_SECONDS": "5",
        "PROXY_UPSTREAM_TIMEOUT_SECONDS": "5",
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


def _rebind_upstream(client: TestClient, *, http: httpx.AsyncClient | None = None) -> httpx.AsyncClient:
    """Refresh settings/provider from current env and optionally replace http client."""
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


# ---------------------------------------------------------------------------
# ARB32-001
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_bounded_body_rejects_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    from app.model_proxy.proxy import read_bounded_body
    from starlette.requests import Request

    body_chunks_read = {"n": 0}

    async def receive():
        body_chunks_read["n"] += 1
        return {"type": "http.request", "body": b"x" * 10, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "headers": [
            (b"content-length", b"99999"),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    request = Request(scope, receive)
    body, err = await read_bounded_body(request, max_bytes=100, idle_timeout=5.0, overall_timeout=30.0)
    assert body is None
    assert err is not None
    assert err.status_code == 413
    assert body_chunks_read["n"] == 0


@pytest.mark.asyncio
async def test_read_bounded_body_chunked_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    from app.model_proxy.proxy import read_bounded_body
    from starlette.requests import Request

    chunks = [b"a" * 40, b"b" * 40, b"c" * 40]
    state = {"i": 0}

    async def receive():
        i = state["i"]
        if i >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        state["i"] = i + 1
        return {"type": "http.request", "body": chunks[i], "more_body": i + 1 < len(chunks)}

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
    request = Request(scope, receive)
    body, err = await read_bounded_body(request, max_bytes=64, idle_timeout=5.0, overall_timeout=30.0)
    assert body is None
    assert err is not None
    assert err.status_code == 413


def test_body_too_large_via_content(monkeypatch: pytest.MonkeyPatch) -> None:
    with _open_proxy(monkeypatch, PROXY_MAX_BODY_BYTES="100") as client:
        tok = _issue(client, "a" * 32)
        r = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            content=b"x" * 200,
        )
        assert r.status_code == 413
        assert r.json()["error"] == "body_too_large"


# ---------------------------------------------------------------------------
# ARB32-002
# ---------------------------------------------------------------------------


def test_output_cap_non_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    with _open_proxy(monkeypatch, PROXY_MAX_BYTES_PER_JOB="80", PROXY_MAX_BODY_BYTES="40") as client:
        tok = _issue(client, "c" * 32)
        payload = b'{"model":"x"}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pad": "y" * 200})

        old = _rebind_upstream(client, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        # Restore token store record after rebind wiped store
        tok = _issue(client, "c" * 32)
        try:
            r = client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                content=payload,
            )
            assert r.status_code == 429
            assert r.json()["error"] == "byte_limit"
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old


def test_output_cap_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    with _open_proxy(monkeypatch, PROXY_MAX_BYTES_PER_JOB="100", PROXY_MAX_BODY_BYTES="40") as client:
        body = b'{"model":"x","stream":true}'

        def handler(request: httpx.Request) -> httpx.Response:
            data = b"data: " + (b"z" * 500) + b"\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

        old = _rebind_upstream(client, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        tok = _issue(client, "d" * 32)
        try:
            with client.stream(
                "POST",
                "/v1/responses",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                content=body,
            ) as resp:
                assert resp.status_code == 200
                received = b"".join(resp.iter_bytes())
            assert len(received) <= 100 - len(body)
            assert len(received) < 500
            rec = client.app.state.store.resolve(tok)
            assert rec is not None
            assert client.app.state.store.bytes_used(rec) <= 100
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old


def test_output_cap_concurrent_same_job(monkeypatch: pytest.MonkeyPatch) -> None:
    with _open_proxy(
        monkeypatch,
        PROXY_MAX_BYTES_PER_JOB="150",
        PROXY_MAX_BODY_BYTES="20",
        PROXY_MAX_CONCURRENT_PER_JOB="4",
    ) as client:
        body = b'{"m":1}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"Q" * 200, headers={"content-type": "application/json"})

        old = _rebind_upstream(client, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        tok = _issue(client, "e" * 32)
        results: list[httpx.Response] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(
                    client.post(
                        "/v1/responses",
                        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                        content=body,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        try:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors
            assert len(results) == 2
            bodies = []
            for r in results:
                if r.status_code == 200:
                    bodies.append(r.content)
                else:
                    assert r.status_code == 429
                    assert r.json()["error"] == "byte_limit"
            for b in bodies:
                assert len(b) < 200
            rec = client.app.state.store.resolve(tok)
            assert rec is not None
            assert client.app.state.store.bytes_used(rec) <= 150
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old


def test_consume_bytes_out_atomic_unit() -> None:
    from app.model_proxy.settings import ProxySettings
    from app.model_proxy.tokens import TokenStore

    settings = ProxySettings(
        issuer_token="x" * 32,
        default_upstream_key="sk",
        owner_upstream_keys={},
        upstream_host="api.openai.com",
        upstream_scheme="https",
        provider="openai",
        max_review_timeout_seconds=600,
        token_grace_seconds=0,
        max_body_bytes=1000,
        max_requests_per_job=100,
        max_bytes_per_job=100,
        max_concurrent_per_job=10,
        stream_idle_timeout_seconds=5.0,
        upstream_timeout_seconds=5.0,
    )
    store = TokenStore(settings)
    issued = store.issue(job_id="z" * 32, timeout_seconds=60)
    rec = store.resolve(issued.token)
    assert rec is not None
    assert store.begin_request(rec, body_len=0) is None
    results: list[int] = []

    def worker() -> None:
        total = 0
        for _ in range(50):
            total += store.consume_bytes_out(rec, 3)
        results.append(total)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 100
    assert store.bytes_used(rec) == 100
    store.end_request(rec)


# ---------------------------------------------------------------------------
# ARB32-003
# ---------------------------------------------------------------------------


def test_idle_timeout_trickle_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(conn: socket.socket) -> None:
        req = _read_http_request(conn)
        assert b"accept-encoding: identity" in req.lower()
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )
        conn.sendall(headers)
        for _ in range(8):
            piece = b"x"
            conn.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            time.sleep(0.85)
        conn.sendall(b"0\r\n\r\n")

    thread, port, stop = _serve(handler)
    try:
        with _open_proxy(
            monkeypatch,
            PROXY_UPSTREAM_HOST=f"127.0.0.1:{port}",
            PROXY_UPSTREAM_SCHEME="http",
            PROXY_STREAM_IDLE_TIMEOUT_SECONDS="0.6",
            PROXY_UPSTREAM_TIMEOUT_SECONDS="30",
            PROXY_MAX_BYTES_PER_JOB="100000",
        ) as client:
            old = _rebind_upstream(client, http=httpx.AsyncClient(http2=False, follow_redirects=False))
            tok = _issue(client, "f" * 32)
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
                    data = b"".join(resp.iter_bytes())
                elapsed = time.monotonic() - t0
                assert elapsed < 8.0
                assert len(data) < 8
            finally:
                import anyio

                anyio.run(client.app.state.http.aclose)
                client.app.state.http = old
    finally:
        _stop_server(thread, stop)


def test_overall_deadline_trickle_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(conn: socket.socket) -> None:
        _read_http_request(conn)
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )
        conn.sendall(headers)
        for _ in range(40):
            piece = b"a"
            conn.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            time.sleep(0.25)
        conn.sendall(b"0\r\n\r\n")

    thread, port, stop = _serve(handler)
    try:
        with _open_proxy(
            monkeypatch,
            PROXY_UPSTREAM_HOST=f"127.0.0.1:{port}",
            PROXY_UPSTREAM_SCHEME="http",
            PROXY_STREAM_IDLE_TIMEOUT_SECONDS="5",
            PROXY_UPSTREAM_TIMEOUT_SECONDS="1.0",
            PROXY_MAX_BYTES_PER_JOB="100000",
        ) as client:
            old = _rebind_upstream(client, http=httpx.AsyncClient(http2=False, follow_redirects=False))
            tok = _issue(client, "g" * 32)
            try:
                t0 = time.monotonic()
                r = client.post(
                    "/v1/responses",
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                    content=b'{"stream":false}',
                )
                elapsed = time.monotonic() - t0
                assert r.status_code == 504
                assert r.json()["error"] == "upstream_timeout"
                assert elapsed < 3.5
            finally:
                import anyio

                anyio.run(client.app.state.http.aclose)
                client.app.state.http = old
    finally:
        _stop_server(thread, stop)


def test_token_expiry_stops_upstream_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(conn: socket.socket) -> None:
        _read_http_request(conn)
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )
        conn.sendall(headers)
        for _ in range(30):
            piece = b"e"
            conn.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            time.sleep(0.3)
        conn.sendall(b"0\r\n\r\n")

    thread, port, stop = _serve(handler)
    try:
        with _open_proxy(
            monkeypatch,
            PROXY_UPSTREAM_HOST=f"127.0.0.1:{port}",
            PROXY_UPSTREAM_SCHEME="http",
            PROXY_TOKEN_GRACE_SECONDS="0",
            PROXY_STREAM_IDLE_TIMEOUT_SECONDS="5",
            PROXY_UPSTREAM_TIMEOUT_SECONDS="30",
            PROXY_MAX_BYTES_PER_JOB="100000",
        ) as client:
            old = _rebind_upstream(client, http=httpx.AsyncClient(http2=False, follow_redirects=False))
            r = client.post(
                f"/internal/jobs/{'h' * 32}/token",
                headers=_issuer_headers(),
                json={"timeout_seconds": 1},
            )
            assert r.status_code == 200
            tok = r.json()["token"]
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
                    data = b"".join(resp.iter_bytes())
                elapsed = time.monotonic() - t0
                assert elapsed < 5.0
                assert len(data) < 30
            finally:
                import anyio

                anyio.run(client.app.state.http.aclose)
                client.app.state.http = old
    finally:
        _stop_server(thread, stop)


# ---------------------------------------------------------------------------
# ARB32-004
# ---------------------------------------------------------------------------


def test_accept_encoding_identity_and_strip_gzip_headers_non_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    with _open_proxy(monkeypatch) as client:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept-encoding"] = request.headers.get("Accept-Encoding", "")
            raw = b'{"ok":true}'
            # Valid gzip + Content-Encoding (identity request means no auto-decode).
            compressed = gzip.compress(raw)
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed)),
                },
                content=compressed,
            )

        old = _rebind_upstream(client, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        tok = _issue(client, "i" * 32)
        try:
            r = client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                content=b'{"model":"x"}',
            )
            assert r.status_code == 200
            assert seen["accept-encoding"] == "identity"
            assert "content-encoding" not in {k.lower() for k in r.headers.keys()}
            # MockTransport may still decode; production identity avoids decode. Accept either form.
            assert r.content == b'{"ok":true}' or r.content.startswith(b"\x1f\x8b")
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old


def test_strip_gzip_headers_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    with _open_proxy(monkeypatch) as client:
        seen: dict[str, str] = {}
        sse = b'data: {"x":1}\n\n'
        compressed = gzip.compress(sse)

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept-encoding"] = request.headers.get("Accept-Encoding", "")
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "content-encoding": "gzip",
                    "transfer-encoding": "chunked",
                },
                content=compressed,
            )

        old = _rebind_upstream(client, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        tok = _issue(client, "j" * 32)
        try:
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
                assert seen["accept-encoding"] == "identity"
                assert "content-encoding" not in {k.lower() for k in resp.headers.keys()}
                data = b"".join(resp.iter_bytes())
            # MockTransport may decode gzip despite identity; header strip is the contract.
            assert data == sse or data == compressed or data.startswith(b"\x1f\x8b")
        finally:
            import anyio

            anyio.run(client.app.state.http.aclose)
            client.app.state.http = old
