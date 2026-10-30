"""Allowlisted reverse proxy for POST /v1/responses with SSE passthrough."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from app.model_proxy import metrics
from app.model_proxy.providers.base import Provider
from app.model_proxy.settings import ProxySettings
from app.model_proxy.tokens import JobTokenRecord, TokenStore

logger = logging.getLogger(__name__)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_STRIP_RESPONSE_HEADERS = _HOP_BY_HOP | {
    "content-encoding",
    "content-length",
    "transfer-encoding",
}


def allowlist_check(request: Request) -> str | None:
    """Return deny reason or None if allowed."""
    if request.method.upper() == "CONNECT":
        return "connect"
    if request.headers.get("upgrade", "").lower() == "websocket":
        return "websocket"
    path = request.url.path
    if path.startswith("http://") or path.startswith("https://"):
        return "absolute_url"
    if request.method.upper() != "POST":
        return "method"
    if path.rstrip("/") != "/v1/responses":
        return "path"
    return None


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _token_remaining(rec: JobTokenRecord) -> float:
    return max(0.0, float(rec.expires_at) - time.time())


async def read_bounded_body(
    request: Request,
    max_bytes: int,
    *,
    idle_timeout: float,
    overall_timeout: float,
    rec: JobTokenRecord | None = None,
    store: TokenStore | None = None,
) -> tuple[bytes | None, Response | None]:
    """Read the request body with size + wall-clock bounds.

    Rejects oversized ``Content-Length`` before reading. For chunked / unknown
    length, reads ``request.stream()`` incrementally and aborts at ``max_bytes``.
    Each chunk wait is bounded by idle, overall, and (when ``rec`` is set) token
    remaining lifetime via ``asyncio.timeout``.
    Returns ``(body, None)`` on success or ``(None, error_response)``.
    """
    cl_raw = request.headers.get("content-length")
    if cl_raw is not None and cl_raw.strip() != "":
        try:
            content_length = int(cl_raw)
        except ValueError:
            content_length = -1
        if content_length > max_bytes:
            metrics.REQUESTS.labels(result="body_too_large").inc()
            return None, Response(
                status_code=413,
                content='{"error":"body_too_large"}',
                media_type="application/json",
            )

    overall_deadline = time.monotonic() + float(overall_timeout)
    idle = float(idle_timeout)
    chunks: list[bytes] = []
    total = 0
    stream = request.stream()
    aiter = stream.__aiter__()
    while True:
        if store is not None and rec is not None and not store.still_valid(rec):
            metrics.REQUESTS.labels(result="expired").inc()
            return None, Response(
                status_code=401,
                content='{"error":"expired"}',
                media_type="application/json",
            )
        remaining = _remaining(overall_deadline)
        if remaining <= 0:
            metrics.REQUESTS.labels(result="upload_timeout").inc()
            return None, Response(
                status_code=408,
                content='{"error":"upload_timeout"}',
                media_type="application/json",
            )
        token_left = _token_remaining(rec) if rec is not None else remaining
        if rec is not None and token_left <= 0:
            metrics.REQUESTS.labels(result="expired").inc()
            return None, Response(
                status_code=401,
                content='{"error":"expired"}',
                media_type="application/json",
            )
        wait = min(idle, remaining, token_left if rec is not None else remaining)
        if wait <= 0:
            metrics.REQUESTS.labels(result="upload_timeout").inc()
            return None, Response(
                status_code=408,
                content='{"error":"upload_timeout"}',
                media_type="application/json",
            )
        try:
            if rec is not None:
                chunk = await _anext_or_invalidated(aiter, rec, wait)
            else:
                async with asyncio.timeout(wait):
                    chunk = await anext(aiter)
        except StopAsyncIteration:
            break
        except TimeoutError:
            if rec is not None and (rec.invalidated.is_set() or _token_remaining(rec) <= 0):
                metrics.REQUESTS.labels(result="expired").inc()
                return None, Response(
                    status_code=401,
                    content='{"error":"expired"}',
                    media_type="application/json",
                )
            if _remaining(overall_deadline) <= 0 or wait < idle:
                metrics.REQUESTS.labels(result="upload_timeout").inc()
                return None, Response(
                    status_code=408,
                    content='{"error":"upload_timeout"}',
                    media_type="application/json",
                )
            metrics.REQUESTS.labels(result="upload_idle_timeout").inc()
            return None, Response(
                status_code=408,
                content='{"error":"upload_idle_timeout"}',
                media_type="application/json",
            )
        if store is not None and rec is not None and not store.still_valid(rec):
            metrics.REQUESTS.labels(result="expired").inc()
            return None, Response(
                status_code=401,
                content='{"error":"expired"}',
                media_type="application/json",
            )
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            metrics.REQUESTS.labels(result="body_too_large").inc()
            return None, Response(
                status_code=413,
                content='{"error":"body_too_large"}',
                media_type="application/json",
            )
        chunks.append(chunk)
    return b"".join(chunks), None


async def _wait_invalidated(rec: JobTokenRecord, wait: float) -> bool:
    """Poll ``rec.invalidated`` until set or ``wait`` elapses. Cancellable.

    Returns True if invalidated, False on timeout. Uses a short sleep so a sync
    ``TokenStore.revoke`` (threadpool) wakes the waiter without blocking a worker
    thread for the full idle window.
    """
    deadline = time.monotonic() + float(wait)
    while time.monotonic() < deadline:
        if rec.invalidated.is_set():
            return True
        await asyncio.sleep(0.05)
    return rec.invalidated.is_set()


async def _anext_or_invalidated(
    aiter: AsyncIterator[bytes],
    rec: JobTokenRecord,
    wait: float,
) -> bytes:
    """Race ``anext(aiter)`` against token invalidation (revoke/expiry signal).

    Raises ``TimeoutError`` on idle/overall slice expiry, or when invalidated
    (caller distinguishes via ``rec.invalidated`` / ``still_valid``).
    Raises ``StopAsyncIteration`` on clean EOF.
    """
    if rec.invalidated.is_set():
        raise TimeoutError("expired")

    anext_task = asyncio.create_task(anext(aiter))  # type: ignore[arg-type]
    inv_task = asyncio.create_task(_wait_invalidated(rec, wait))
    try:
        done, pending = await asyncio.wait(
            {anext_task, inv_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):  # noqa: BLE001
                pass

        if inv_task in done and not inv_task.cancelled():
            invalidated = bool(inv_task.result())
            if invalidated or rec.invalidated.is_set():
                raise TimeoutError("expired")
            # Poll timed out without invalidation.
            if anext_task in done and not anext_task.cancelled():
                return anext_task.result()
            raise TimeoutError("idle")

        return anext_task.result()
    except StopAsyncIteration:
        raise
    except Exception:
        if not anext_task.done():
            anext_task.cancel()
            try:
                await anext_task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):  # noqa: BLE001
                pass
        raise


def _forward_response_headers(response: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in response.headers.items():
        low = key.lower()
        if low in _STRIP_RESPONSE_HEADERS:
            continue
        if low == "authorization":
            continue
        out[key] = value
    return out


async def forward_responses(
    *,
    request: Request,
    body: bytes,
    settings: ProxySettings,
    provider: Provider,
    store: TokenStore,
    rec: JobTokenRecord,
    upstream_api_key: str,
    client: httpx.AsyncClient,
) -> Response:
    """Forward POST /v1/responses under a slot owned by the caller.

    Slot ownership:
    - On non-stream ``Response`` return / error return: caller keeps ownership and
      must ``end_request`` (this function does **not** release).
    - On ``StreamingResponse`` return: ownership transfers to ``stream_body`` which
      releases in its ``finally`` (exactly once after the stream ends).
    - On cancellation / exception before returning: caller releases.
    """
    reason = allowlist_check(request)
    if reason:
        metrics.ALLOWLIST_DENY.labels(reason=reason).inc()
        metrics.REQUESTS.labels(result="allowlist_deny").inc()
        return Response(status_code=403, content='{"error":"forbidden"}', media_type="application/json")

    if len(body) > settings.max_body_bytes:
        metrics.REQUESTS.labels(result="body_too_large").inc()
        return Response(status_code=413, content='{"error":"body_too_large"}', media_type="application/json")

    target = provider.responses_target()
    headers = {
        "Authorization": f"Bearer {upstream_api_key}",
        "Content-Type": request.headers.get("content-type") or "application/json",
        "Accept": request.headers.get("accept") or "application/json",
        "Accept-Encoding": "identity",
    }
    for name in ("openai-beta", "user-agent", "originator", "x-codex-beta-features"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    overall_deadline = time.monotonic() + float(settings.upstream_timeout_seconds)
    idle = float(settings.stream_idle_timeout_seconds)
    httpx_timeout = httpx.Timeout(
        connect=min(10.0, float(settings.upstream_timeout_seconds)),
        read=max(idle, 1.0),
        write=60.0,
        pool=10.0,
    )

    response: httpx.Response | None = None
    try:
        if not store.still_valid(rec):
            metrics.REQUESTS.labels(result="expired").inc()
            return Response(status_code=401, content='{"error":"expired"}', media_type="application/json")

        upstream = client.build_request(
            "POST",
            target.url,
            headers=headers,
            content=body,
            timeout=httpx_timeout,
        )
        budget = min(_remaining(overall_deadline), _token_remaining(rec))
        if budget <= 0:
            metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
            metrics.REQUESTS.labels(result="upstream_timeout").inc()
            return Response(
                status_code=504,
                content='{"error":"upstream_timeout"}',
                media_type="application/json",
            )
        try:
            async with asyncio.timeout(budget):
                # Also abort promptly if revoked while waiting for headers.
                send_task = asyncio.create_task(client.send(upstream, stream=True))
                inv_task = asyncio.create_task(_wait_invalidated(rec, budget))
                done, pending = await asyncio.wait(
                    {send_task, inv_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                if (
                    inv_task in done
                    and not inv_task.cancelled()
                    and (bool(inv_task.result()) or rec.invalidated.is_set())
                ):
                    if send_task.done() and not send_task.cancelled():
                        try:
                            resp_obj = send_task.result()
                            await resp_obj.aclose()
                        except Exception:  # noqa: BLE001
                            pass
                    metrics.REQUESTS.labels(result="expired").inc()
                    return Response(
                        status_code=401,
                        content='{"error":"expired"}',
                        media_type="application/json",
                    )
                response = send_task.result()
        except TimeoutError:
            metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
            metrics.REQUESTS.labels(result="upstream_timeout").inc()
            return Response(
                status_code=504,
                content='{"error":"upstream_timeout"}',
                media_type="application/json",
            )
    except httpx.TimeoutException:
        metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
        metrics.REQUESTS.labels(result="upstream_timeout").inc()
        return Response(status_code=504, content='{"error":"upstream_timeout"}', media_type="application/json")
    except httpx.HTTPError as exc:
        metrics.UPSTREAM_ERRORS.labels(kind="http_error").inc()
        metrics.REQUESTS.labels(result="upstream_error").inc()
        logger.warning("model_proxy upstream error job_id=%s: %s", rec.job_id, type(exc).__name__)
        return Response(status_code=502, content='{"error":"upstream_error"}', media_type="application/json")

    assert response is not None
    metrics.BYTES.labels(direction="in").inc(len(body))
    content_type = response.headers.get("content-type", "application/json")
    is_stream = "text/event-stream" in content_type.lower()
    out_headers = _forward_response_headers(response)

    async def _close_upstream() -> None:
        try:
            await response.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def _next_chunk(aiter: AsyncIterator[bytes]) -> bytes | None:
        """Wait for next upstream chunk; revoke interrupts even when upstream is silent."""
        if not store.still_valid(rec):
            await _close_upstream()
            return None
        remaining = _remaining(overall_deadline)
        token_left = _token_remaining(rec)
        if remaining <= 0:
            await _close_upstream()
            raise TimeoutError("overall")
        if token_left <= 0 or rec.invalidated.is_set():
            await _close_upstream()
            raise TimeoutError("expired")
        wait = min(idle, remaining, token_left)
        if wait <= 0:
            await _close_upstream()
            raise TimeoutError("overall")
        try:
            chunk = await _anext_or_invalidated(aiter, rec, wait)
        except StopAsyncIteration:
            return None
        except TimeoutError as exc:
            await _close_upstream()
            if rec.invalidated.is_set() or not store.still_valid(rec) or _token_remaining(rec) <= 0:
                raise TimeoutError("expired") from exc
            if _remaining(overall_deadline) <= 0 or wait < idle:
                raise TimeoutError("overall") from exc
            raise TimeoutError("idle") from exc
        if not store.still_valid(rec):
            await _close_upstream()
            return None
        return chunk

    async def stream_body() -> AsyncIterator[bytes]:
        aiter = response.aiter_bytes()
        result_label = "ok" if response.status_code < 400 else "upstream_status"
        try:
            while True:
                try:
                    chunk = await _next_chunk(aiter)
                except TimeoutError as exc:
                    kind = str(exc) or "timeout"
                    if kind == "idle":
                        metrics.UPSTREAM_ERRORS.labels(kind="idle_timeout").inc()
                        result_label = "idle_timeout"
                    elif kind == "expired":
                        result_label = "expired"
                    else:
                        metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
                        result_label = "upstream_timeout"
                    return
                if chunk is None:
                    if not store.still_valid(rec):
                        result_label = "expired"
                    return
                if not chunk:
                    continue
                accepted = store.consume_bytes_out(rec, len(chunk))
                if accepted <= 0:
                    await _close_upstream()
                    result_label = "byte_limit"
                    return
                if accepted < len(chunk):
                    metrics.BYTES.labels(direction="out").inc(accepted)
                    yield chunk[:accepted]
                    await _close_upstream()
                    result_label = "byte_limit"
                    return
                metrics.BYTES.labels(direction="out").inc(accepted)
                yield chunk
        finally:
            await _close_upstream()
            store.end_request(rec)
            metrics.REQUESTS.labels(result=result_label).inc()
            metrics.ACTIVE_JOBS.set(store.active_job_count())

    if is_stream:
        # Ownership transfers to stream_body.finally on successful return.
        return StreamingResponse(
            stream_body(),
            status_code=response.status_code,
            headers=out_headers,
            media_type=content_type,
        )

    # Non-stream: caller retains slot ownership; we never end_request here.
    chunks: list[bytes] = []
    aiter = response.aiter_bytes()
    try:
        while True:
            try:
                chunk = await _next_chunk(aiter)
            except TimeoutError as exc:
                kind = str(exc) or "timeout"
                if kind == "idle":
                    metrics.UPSTREAM_ERRORS.labels(kind="idle_timeout").inc()
                    metrics.REQUESTS.labels(result="idle_timeout").inc()
                    return Response(
                        status_code=504,
                        content='{"error":"idle_timeout"}',
                        media_type="application/json",
                    )
                if kind == "expired":
                    metrics.REQUESTS.labels(result="expired").inc()
                    return Response(
                        status_code=401,
                        content='{"error":"expired"}',
                        media_type="application/json",
                    )
                metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
                metrics.REQUESTS.labels(result="upstream_timeout").inc()
                return Response(
                    status_code=504,
                    content='{"error":"upstream_timeout"}',
                    media_type="application/json",
                )
            if chunk is None:
                # ARB32-007: invalidation with buffered chunks → discard, explicit 401.
                if not store.still_valid(rec):
                    chunks.clear()
                    metrics.REQUESTS.labels(result="expired").inc()
                    return Response(
                        status_code=401,
                        content='{"error":"expired"}',
                        media_type="application/json",
                    )
                break
            if not chunk:
                continue
            accepted = store.consume_bytes_out(rec, len(chunk))
            if accepted < len(chunk):
                await _close_upstream()
                metrics.REQUESTS.labels(result="byte_limit").inc()
                return Response(
                    status_code=429,
                    content='{"error":"byte_limit"}',
                    media_type="application/json",
                )
            chunks.append(chunk)
            metrics.BYTES.labels(direction="out").inc(accepted)

        data = b"".join(chunks)
        metrics.REQUESTS.labels(result="ok" if response.status_code < 400 else "upstream_status").inc()
        metrics.ACTIVE_JOBS.set(store.active_job_count())
        return Response(
            content=data,
            status_code=response.status_code,
            headers=out_headers,
            media_type=content_type,
        )
    finally:
        await _close_upstream()
