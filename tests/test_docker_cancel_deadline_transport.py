"""AR-030-001: cancel Docker calls use wall-clock deadline, not HTTPX inactivity alone.

Starts a real local TCP HTTP server that trickles chunks (or delays headers) so a
float HTTPX timeout would *not* fire between chunks, while the absolute cancel
budget must still interrupt the request and close the connection.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
from app.launcher.docker_api import DockerClient, DockerDeadlineExceeded
from app.launcher.jobs import cancel_review_job
from app.launcher.timeouts import (
    WORKER_CANCEL_HTTP_READ_SECONDS,
    launcher_cancel_deadline_seconds,
)
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def _serve_once(handler, *, ready: threading.Event) -> tuple[threading.Thread, int]:
    """Bind 127.0.0.1:0 and serve connections with ``handler(conn)`` until stopped."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = int(sock.getsockname()[1])
    stop = threading.Event()

    def _run() -> None:
        ready.set()
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
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

    thread = threading.Thread(target=_run, name="fake-docker-http", daemon=True)
    thread.start()
    if not ready.wait(timeout=2):
        stop.set()
        raise RuntimeError("fake docker server failed to start")
    # Attach stop on thread for callers
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.server_sock = sock  # type: ignore[attr-defined]
    return thread, port


def _read_request(conn: socket.socket, *, limit: int = 65536) -> bytes:
    conn.settimeout(2.0)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < limit:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _trickle_containers_json(conn: socket.socket, *, chunk_gap: float, chunks: int) -> None:
    """HTTP/1.1 chunked JSON array sent one character at a time with gaps.

    Gaps are short enough that an HTTPX *inactivity* read timeout equal to the
    cancel budget would reset and never fire, while wall-clock time exceeds the budget.
    """
    _read_request(conn)
    body = b"[]"
    # Pad so we need many slow chunks (wall clock >> budget, gaps << budget).
    payload = body + (b" " * max(0, chunks - len(body)))
    headers = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    )
    conn.sendall(headers)
    for i in range(0, len(payload), 1):
        piece = payload[i : i + 1]
        # chunked: size CRLF data CRLF
        conn.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        time.sleep(chunk_gap)
    conn.sendall(b"0\r\n\r\n")


def _slow_headers(conn: socket.socket, *, delay: float) -> None:
    _read_request(conn)
    time.sleep(delay)
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]"
    )


def _slow_accept_bind(delay: float) -> tuple[threading.Thread, int, threading.Event]:
    """Listen but delay ``accept`` handling so connect stalls from the client view.

    Actually TCP accept is immediate; we delay before reading/responding so the
    client sits in connect+first-byte wait. For a stronger connect stall we
    fill the listen backlog — simpler: sleep before any response bytes.
    """
    ready = threading.Event()

    def handler(conn: socket.socket) -> None:
        time.sleep(delay)
        try:
            _read_request(conn)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]"
            )
        except OSError:
            pass

    thread, port = _serve_once(handler, ready=ready)
    return thread, port, ready


@pytest.fixture()
def stop_server():
    threads: list[threading.Thread] = []

    def _track(thread: threading.Thread) -> threading.Thread:
        threads.append(thread)
        return thread

    yield _track
    for thread in threads:
        stop = getattr(thread, "stop_event", None)
        if stop is not None:
            stop.set()
        thread.join(timeout=2)


def test_list_containers_wall_deadline_beats_trickle(
    monkeypatch: pytest.MonkeyPatch,
    stop_server,
) -> None:
    """Trickled body would defeat inactivity timeout; wall deadline must still win."""
    ready = threading.Event()
    # 0.25s between bytes, 40 bytes → ~10s if fully read. Budget 0.8s must cut it off.
    chunk_gap = 0.25

    def handler(conn: socket.socket) -> None:
        _trickle_containers_json(conn, chunk_gap=chunk_gap, chunks=40)

    thread, port = _serve_once(handler, ready=ready)
    stop_server(thread)
    monkeypatch.setenv("DOCKER_HOST", f"http://127.0.0.1:{port}")

    client = DockerClient()
    started = time.monotonic()
    with pytest.raises(DockerDeadlineExceeded):
        client.list_containers_for_job("a" * 32, timeout=0.8)
    elapsed = time.monotonic() - started
    client.close()

    assert elapsed < 2.0, f"wall deadline did not interrupt trickle (elapsed={elapsed:.3f}s)"
    assert elapsed >= 0.7, f"returned too early (elapsed={elapsed:.3f}s)"


def test_list_containers_wall_deadline_slow_headers(
    monkeypatch: pytest.MonkeyPatch,
    stop_server,
) -> None:
    ready = threading.Event()

    def handler(conn: socket.socket) -> None:
        _slow_headers(conn, delay=5.0)

    thread, port = _serve_once(handler, ready=ready)
    stop_server(thread)
    monkeypatch.setenv("DOCKER_HOST", f"http://127.0.0.1:{port}")

    client = DockerClient()
    started = time.monotonic()
    with pytest.raises(DockerDeadlineExceeded):
        client.list_containers_for_job("b" * 32, timeout=0.6)
    elapsed = time.monotonic() - started
    client.close()

    assert elapsed < 2.0
    assert elapsed >= 0.5


def test_cancel_delete_returns_unconfirmed_under_trickle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_server,
) -> None:
    """Full DELETE cancel against a trickling Docker stays under worker read timeout."""
    deadline = 1.0
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", str(deadline))
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "0.3")
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))

    ready = threading.Event()
    chunk_gap = 0.2

    def handler(conn: socket.socket) -> None:
        # Any Docker verb: trickle so inactivity timeout would not fire within budget.
        _trickle_containers_json(conn, chunk_gap=chunk_gap, chunks=80)

    thread, port = _serve_once(handler, ready=ready)
    stop_server(thread)
    monkeypatch.setenv("DOCKER_HOST", f"http://127.0.0.1:{port}")

    from app.launcher.auth import require_launcher_auth
    from app.launcher.main import cancel_job

    app = FastAPI()
    app.add_api_route(
        "/v1/jobs/{job_id}",
        cancel_job,
        methods=["DELETE"],
        dependencies=[Depends(require_launcher_auth)],
    )
    http = TestClient(app)
    jid = "c" * 32
    token = "test-launcher-token-32chars-ok"

    started = time.monotonic()
    response = http.delete(
        f"/v1/jobs/{jid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == jid
    assert body["confirmed"] is False  # deadline → worker keeps staging
    # Must finish within cancel deadline + small scheduling epsilon, and well under worker 5s.
    assert elapsed < deadline + 0.75, f"cancel too slow: {elapsed:.3f}s (deadline={deadline})"
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    assert launcher_cancel_deadline_seconds() == deadline


def test_cancel_review_job_direct_trickle_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
    stop_server,
) -> None:
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "0.9")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "0.2")
    ready = threading.Event()

    def handler(conn: socket.socket) -> None:
        _trickle_containers_json(conn, chunk_gap=0.2, chunks=60)

    thread, port = _serve_once(handler, ready=ready)
    stop_server(thread)
    monkeypatch.setenv("DOCKER_HOST", f"http://127.0.0.1:{port}")

    started = time.monotonic()
    result = cancel_review_job(job_id="d" * 32)
    elapsed = time.monotonic() - started

    assert result["confirmed"] is False
    assert elapsed < 0.9 + 0.75
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
