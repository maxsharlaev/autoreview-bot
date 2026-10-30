"""ARB32-009: cancellation-safe race cancels upstream send / chunk wait tasks."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from app.model_proxy.proxy import (
    _anext_or_invalidated,
    _race_primary_or_invalidated,
)
from app.model_proxy.tokens import JobTokenRecord


def _rec(*, ttl: float = 60.0) -> JobTokenRecord:
    import time

    return JobTokenRecord(
        token_hash="b" * 64,
        job_id="a" * 32,
        owner_id="",
        expires_at=time.time() + ttl,
    )


class _FakeResponse:
    """Minimal stand-in for httpx.Response used to verify on_discard/aclose."""

    def __init__(self) -> None:
        self.closed = False
        self.close_event = asyncio.Event()

    async def aclose(self) -> None:
        self.closed = True
        self.close_event.set()


@pytest.mark.asyncio
async def test_race_timeout_cancels_primary_and_discards_late_response() -> None:
    """Wait expiry cancels the hanging primary; a late result is discarded via on_discard."""
    cancelled = asyncio.Event()
    late = _FakeResponse()
    discarded: list[Any] = []

    async def hang_then_respond() -> _FakeResponse:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return late

    async def on_discard(resp: _FakeResponse) -> None:
        discarded.append(resp)
        await resp.aclose()

    rec = _rec()
    t0 = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError, match="idle"):
        await _race_primary_or_invalidated(
            hang_then_respond(),
            rec,
            0.15,
            on_discard=on_discard,
        )
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 1.0
    assert cancelled.is_set()
    # Primary was cancelled before producing a value — nothing to discard.
    assert discarded == []
    assert not late.closed

    # No leftover tasks from the race.
    await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
    # Filter out pytest-asyncio internals loosely: none should mention hang_then
    assert not any("hang_then" in (t.get_coro().__qualname__ if t.get_coro() else "") for t in pending)


@pytest.mark.asyncio
async def test_race_invalidation_discards_completed_late_response() -> None:
    """When primary finishes but invalidation also fires, late response is aclosed."""
    resp = _FakeResponse()
    rec = _rec()

    async def complete_and_invalidate() -> _FakeResponse:
        rec.invalidated.set()
        return resp

    async def on_discard(r: _FakeResponse) -> None:
        await r.aclose()

    with pytest.raises(TimeoutError, match="expired"):
        await _race_primary_or_invalidated(
            complete_and_invalidate(),
            rec,
            2.0,
            on_discard=on_discard,
        )
    assert resp.closed


@pytest.mark.asyncio
async def test_race_revoke_cancels_primary() -> None:
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def hang() -> str:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "nope"

    rec = _rec()

    async def revoker() -> None:
        await started.wait()
        await asyncio.sleep(0.05)
        rec.invalidated.set()

    rev = asyncio.create_task(revoker())
    with pytest.raises(TimeoutError, match="expired"):
        await _race_primary_or_invalidated(hang(), rec, 5.0)
    await rev
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_race_client_cancel_cancels_primary_and_discards() -> None:
    cancelled = asyncio.Event()
    started = asyncio.Event()
    resp = _FakeResponse()
    discard_called = asyncio.Event()

    async def hang_or_late() -> _FakeResponse:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return resp

    async def on_discard(r: _FakeResponse) -> None:
        await r.aclose()
        discard_called.set()

    rec = _rec()

    async def run() -> _FakeResponse:
        return await _race_primary_or_invalidated(
            hang_or_late(),
            rec,
            30.0,
            on_discard=on_discard,
        )

    task = asyncio.create_task(run())
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    # Hanging primary never produced a response to discard.
    assert not discard_called.is_set()


@pytest.mark.asyncio
async def test_forward_header_wait_timeout_cancels_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """Header-wait TimeoutError path cancels client.send (no orphan upstream)."""
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "test-issuer-token-32chars-ok!!")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "30")
    monkeypatch.setenv("PROXY_MAX_BODY_BYTES", "65536")
    monkeypatch.setenv("PROXY_MAX_REQUESTS_PER_JOB", "50")
    monkeypatch.setenv("PROXY_MAX_BYTES_PER_JOB", "200000")
    monkeypatch.setenv("PROXY_MAX_CONCURRENT_PER_JOB", "2")
    monkeypatch.setenv("PROXY_STREAM_IDLE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("PROXY_UPSTREAM_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("PROXY_UPSTREAM_HOST", "api.openai.com")
    monkeypatch.setenv("PROXY_UPSTREAM_SCHEME", "https")
    monkeypatch.setenv("PROXY_PROVIDER", "openai")

    from app.model_proxy.providers import build_provider
    from app.model_proxy.proxy import forward_responses
    from app.model_proxy.settings import load_settings
    from app.model_proxy.tokens import TokenStore
    from starlette.requests import Request

    settings = load_settings()
    store = TokenStore(settings)
    provider = build_provider(settings)
    issued = store.issue(job_id="t" * 32, timeout_seconds=60)
    rec = store.resolve(issued.token)
    assert rec is not None
    assert store.begin_request(rec, body_len=0) is None

    send_cancelled = asyncio.Event()
    send_started = asyncio.Event()

    class _ProxyClient:
        def build_request(self, *a, **k):  # noqa: ANN002, ANN003
            return httpx.AsyncClient().build_request(*a, **k)

        async def send(self, request, stream=False):  # noqa: ANN001
            send_started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                send_cancelled.set()
                raise
            raise AssertionError("unreachable")

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
    resp = await forward_responses(
        request=request,
        body=b'{"model":"x"}',
        settings=settings,
        provider=provider,
        store=store,
        rec=rec,
        upstream_api_key="sk-test",
        client=_ProxyClient(),  # type: ignore[arg-type]
    )
    assert resp.status_code == 504
    assert send_started.is_set()
    # Race finally must have cancelled send.
    assert send_cancelled.is_set()
    store.end_request(rec, refund_request=False)


@pytest.mark.asyncio
async def test_forward_header_wait_revoke_cancels_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "test-issuer-token-32chars-ok!!")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "30")
    monkeypatch.setenv("PROXY_MAX_BODY_BYTES", "65536")
    monkeypatch.setenv("PROXY_MAX_REQUESTS_PER_JOB", "50")
    monkeypatch.setenv("PROXY_MAX_BYTES_PER_JOB", "200000")
    monkeypatch.setenv("PROXY_MAX_CONCURRENT_PER_JOB", "2")
    monkeypatch.setenv("PROXY_STREAM_IDLE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("PROXY_UPSTREAM_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("PROXY_UPSTREAM_HOST", "api.openai.com")
    monkeypatch.setenv("PROXY_UPSTREAM_SCHEME", "https")
    monkeypatch.setenv("PROXY_PROVIDER", "openai")

    from app.model_proxy.providers import build_provider
    from app.model_proxy.proxy import forward_responses
    from app.model_proxy.settings import load_settings
    from app.model_proxy.tokens import TokenStore
    from starlette.requests import Request

    settings = load_settings()
    store = TokenStore(settings)
    provider = build_provider(settings)
    issued = store.issue(job_id="r" * 32, timeout_seconds=60)
    rec = store.resolve(issued.token)
    assert rec is not None
    assert store.begin_request(rec, body_len=0) is None

    send_cancelled = asyncio.Event()
    send_started = asyncio.Event()

    class _ProxyClient:
        def build_request(self, *a, **k):  # noqa: ANN002, ANN003
            return httpx.AsyncClient().build_request(*a, **k)

        async def send(self, request, stream=False):  # noqa: ANN001
            send_started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                send_cancelled.set()
                raise
            raise AssertionError("unreachable")

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

    async def revoker() -> None:
        await send_started.wait()
        await asyncio.sleep(0.05)
        store.revoke(rec.job_id)

    rev = asyncio.create_task(revoker())
    resp = await forward_responses(
        request=request,
        body=b'{"model":"x"}',
        settings=settings,
        provider=provider,
        store=store,
        rec=rec,
        upstream_api_key="sk-test",
        client=_ProxyClient(),  # type: ignore[arg-type]
    )
    await rev
    assert resp.status_code == 401
    assert send_cancelled.is_set()
    store.end_request(rec, refund_request=False)


@pytest.mark.asyncio
async def test_forward_header_wait_cancel_cancels_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "test-issuer-token-32chars-ok!!")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "30")
    monkeypatch.setenv("PROXY_MAX_BODY_BYTES", "65536")
    monkeypatch.setenv("PROXY_MAX_REQUESTS_PER_JOB", "50")
    monkeypatch.setenv("PROXY_MAX_BYTES_PER_JOB", "200000")
    monkeypatch.setenv("PROXY_MAX_CONCURRENT_PER_JOB", "2")
    monkeypatch.setenv("PROXY_STREAM_IDLE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("PROXY_UPSTREAM_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("PROXY_UPSTREAM_HOST", "api.openai.com")
    monkeypatch.setenv("PROXY_UPSTREAM_SCHEME", "https")
    monkeypatch.setenv("PROXY_PROVIDER", "openai")

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

    send_cancelled = asyncio.Event()
    send_started = asyncio.Event()

    class _ProxyClient:
        def build_request(self, *a, **k):  # noqa: ANN002, ANN003
            return httpx.AsyncClient().build_request(*a, **k)

        async def send(self, request, stream=False):  # noqa: ANN001
            send_started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                send_cancelled.set()
                raise
            raise AssertionError("unreachable")

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

    async def run() -> None:
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

    task = asyncio.create_task(run())
    await send_started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert send_cancelled.is_set()
    store.end_request(rec, refund_request=False)


@pytest.mark.asyncio
async def test_anext_revoke_cancels_underlying_anext() -> None:
    """Chunk-wait revoke cancels the pending anext task."""
    cancelled = asyncio.Event()
    started = asyncio.Event()

    class _HangAiter:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return b"x"

    rec = _rec()

    async def revoker() -> None:
        await started.wait()
        await asyncio.sleep(0.05)
        rec.invalidated.set()

    rev = asyncio.create_task(revoker())
    with pytest.raises(TimeoutError, match="expired"):
        await _anext_or_invalidated(_HangAiter(), rec, 5.0)
    await rev
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_anext_timeout_cancels_underlying_anext() -> None:
    cancelled = asyncio.Event()

    class _HangAiter:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return b"x"

    rec = _rec()
    with pytest.raises(TimeoutError, match="idle"):
        await _anext_or_invalidated(_HangAiter(), rec, 0.15)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_anext_client_cancel_cancels_underlying_anext() -> None:
    cancelled = asyncio.Event()
    started = asyncio.Event()

    class _HangAiter:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return b"x"

    rec = _rec()

    async def run() -> bytes:
        return await _anext_or_invalidated(_HangAiter(), rec, 30.0)

    task = asyncio.create_task(run())
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_race_expiry_via_short_wait_cancels_primary() -> None:
    """Token-lifetime-bounded wait (expiry budget) cancels primary like idle timeout."""
    cancelled = asyncio.Event()

    async def hang() -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "nope"

    # Short wait simulates remaining token TTL budget at header/chunk wait.
    rec = _rec(ttl=0.2)
    with pytest.raises(TimeoutError):
        await _race_primary_or_invalidated(hang(), rec, 0.2)
    assert cancelled.is_set()
