"""Allowlisted reverse proxy for POST /v1/responses with SSE passthrough."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from app.model_proxy import metrics
from app.model_proxy.failures import classify_transport, classify_upstream_status, error_body
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

# Responses API terminal events. EOF before any of them is a broken stream.
# completed is a real success; incomplete and failed finished normally and are not connectivity failures.
_SSE_TERMINALS = ("response.completed", "response.failed", "response.incomplete")
_SSE_MARKERS = tuple(
    marker
    for name in _SSE_TERMINALS
    for marker in (
        f"event: {name}".encode(),
        f"event:{name}".encode(),
        f'"type":"{name}"'.encode(),
        f'"type": "{name}"'.encode(),
    )
)
_SSE_HOLD = max(len(marker) for marker in _SSE_MARKERS) - 1
_SSE_TERMINAL_RANK = {"response.completed": 3, "response.failed": 2, "response.incomplete": 1}


def _sse_terminal(blob: bytes) -> str:
    found = ""
    for name in _SSE_TERMINALS:
        markers = (
            f"event: {name}".encode(),
            f"event:{name}".encode(),
            f'"type":"{name}"'.encode(),
            f'"type": "{name}"'.encode(),
        )
        rank = _SSE_TERMINAL_RANK[name]
        if any(marker in blob for marker in markers) and rank > _SSE_TERMINAL_RANK.get(found, 0):
            found = name
    return found


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


async def _aclose_httpx_response(resp: httpx.Response) -> None:
    try:
        await resp.aclose()
    except Exception:  # noqa: BLE001
        pass


async def _race_primary_or_invalidated[T](
    primary: Coroutine[object, object, T],
    rec: JobTokenRecord,
    wait: float,
    *,
    on_discard: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """Race ``primary`` against token invalidation for up to ``wait`` seconds.

    Cancellation-safe: a ``finally`` always cancels and awaits unfinished tasks.
    If ``primary`` completed with a value that is not returned to the caller
    (timeout, invalidation, or cancellation), ``on_discard`` is awaited on that
    value when provided (e.g. ``httpx.Response.aclose`` for a late upstream
    response).

    Returns the primary result when it finishes first and the token is still
    valid. Raises ``TimeoutError("expired")`` on invalidation and
    ``TimeoutError("idle")`` when the wait elapses without a primary result.
    Primary exceptions (including ``StopAsyncIteration``) propagate via
    ``Task.result()``.
    """
    if rec.invalidated.is_set():
        raise TimeoutError("expired")

    primary_task: asyncio.Task[T] = asyncio.create_task(primary)  # type: ignore[arg-type]
    inv_task = asyncio.create_task(_wait_invalidated(rec, wait))
    keep_result = False
    try:
        done, _pending = await asyncio.wait(
            {primary_task, inv_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        invalidated = rec.invalidated.is_set()
        if inv_task in done and not inv_task.cancelled():
            try:
                invalidated = invalidated or bool(inv_task.result())
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if invalidated:
            raise TimeoutError("expired")

        if primary_task in done and primary_task.cancelled():
            # A cancelled upstream task is the caller's cancellation, not a timeout.
            raise asyncio.CancelledError()

        if primary_task in done and not primary_task.cancelled():
            keep_result = True
            return primary_task.result()

        raise TimeoutError("idle")
    finally:
        for task in (primary_task, inv_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, StopAsyncIteration, Exception):  # noqa: BLE001
                    pass
        if not keep_result and on_discard is not None and primary_task.done() and not primary_task.cancelled():
            if primary_task.exception() is None:
                try:
                    await on_discard(primary_task.result())
                except Exception:  # noqa: BLE001
                    pass


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
    return await _race_primary_or_invalidated(
        anext(aiter),  # type: ignore[arg-type]
        rec,
        wait,
    )


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

    started = time.monotonic()
    overall_deadline = started + float(settings.upstream_timeout_seconds)
    idle = float(settings.stream_idle_timeout_seconds)
    httpx_timeout = httpx.Timeout(
        connect=min(10.0, float(settings.upstream_timeout_seconds)),
        read=max(idle, 1.0),
        write=60.0,
        pool=10.0,
    )

    def _finish(result: str, *, error_kind: str | None = None) -> None:
        if error_kind:
            metrics.UPSTREAM_ERRORS.labels(kind=error_kind).inc()
        elapsed = time.monotonic() - started
        metrics.note_model_attempt(result, elapsed)
        logger.info(
            "model_proxy outcome job_id=%s kind=%s duration_ms=%s",
            rec.job_id,
            result,
            int(elapsed * 1000),
            extra={"job_id": rec.job_id, "kind": result},
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
            _finish("upstream_timeout", error_kind="timeout")
            return Response(
                status_code=504,
                content=error_body("upstream_timeout", "overall_timeout"),
                media_type="application/json",
            )
        try:
            # Shared cancellation-safe race: timeout / revoke / cancel always
            # cancel+await the upstream send and aclose any late response.
            response = await _race_primary_or_invalidated(
                client.send(upstream, stream=True),
                rec,
                budget,
                on_discard=_aclose_httpx_response,
            )
        except TimeoutError:
            if rec.invalidated.is_set() or not store.still_valid(rec):
                metrics.REQUESTS.labels(result="expired").inc()
                return Response(
                    status_code=401,
                    content='{"error":"expired"}',
                    media_type="application/json",
                )
            _finish("upstream_timeout", error_kind="timeout")
            return Response(
                status_code=504,
                content=error_body("upstream_timeout", "overall_timeout"),
                media_type="application/json",
            )
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.info("model_proxy upstream error kind=%s job_id=%s", type(exc).__name__, rec.job_id)
        error, kind, status_code, metric_kind = classify_transport(exc)
        _finish(error, error_kind=metric_kind)
        return Response(status_code=status_code, content=error_body(error, kind), media_type="application/json")

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
        result_label, _outcome = classify_upstream_status(response.status_code)
        cancelled = False
        terminal = ""
        tail = b""

        def _see(piece: bytes) -> None:
            nonlocal terminal, tail
            blob = tail + piece
            found = _sse_terminal(blob)
            if _SSE_TERMINAL_RANK.get(found, 0) > _SSE_TERMINAL_RANK.get(terminal, 0):
                terminal = found
            tail = blob[-_SSE_HOLD:]

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
                except httpx.HTTPError as exc:
                    # Headers were already sent, so the HTTP status cannot change.
                    # The metric still keeps the transport cause (read timeout versus reset).
                    error, _kind, _status_code, metric_kind = classify_transport(exc)
                    metrics.UPSTREAM_ERRORS.labels(kind=metric_kind).inc()
                    result_label = error
                    logger.info(
                        "model_proxy upstream error kind=%s job_id=%s",
                        type(exc).__name__,
                        rec.job_id,
                    )
                    return
                if chunk is None:
                    if not store.still_valid(rec):
                        result_label = "expired"
                    elif result_label == "ok" and terminal == "response.incomplete":
                        result_label = "response_incomplete"
                    elif result_label == "ok" and terminal == "response.failed":
                        result_label = "response_failed"
                    elif result_label == "ok" and terminal != "response.completed":
                        metrics.UPSTREAM_ERRORS.labels(kind="stream_disconnect").inc()
                        result_label = "stream_disconnect"
                    return
                if not chunk:
                    continue
                accepted = store.consume_bytes_out(rec, len(chunk))
                if accepted <= 0:
                    await _close_upstream()
                    result_label = "byte_limit"
                    return
                piece = chunk[:accepted]
                _see(piece)
                metrics.BYTES.labels(direction="out").inc(accepted)
                yield piece
                if accepted < len(chunk):
                    await _close_upstream()
                    result_label = "byte_limit"
                    return
        except (asyncio.CancelledError, GeneratorExit):
            # Client gone after headers. Do not record the in-flight status as a success.
            cancelled = True
            raise
        finally:
            await _close_upstream()
            store.end_request(rec)
            if cancelled:
                result_label = "cancelled"
            elif result_label in ("expired", "byte_limit"):
                metrics.REQUESTS.labels(result=result_label).inc()
            else:
                metrics.note_model_attempt(result_label, time.monotonic() - started)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "model_proxy outcome job_id=%s kind=%s duration_ms=%s",
                rec.job_id,
                result_label,
                elapsed_ms,
                extra={"job_id": rec.job_id, "kind": result_label},
            )
            metrics.ACTIVE_JOBS.set(store.active_job_count())

    if response.status_code >= 500:
        # Codex only classifies the body it can log. Keep the status, replace the body
        # with the stable token so the review becomes CODEX_UPSTREAM_UNAVAILABLE.
        await _close_upstream()
        _finish("upstream_5xx")
        return Response(
            status_code=response.status_code,
            content=error_body("upstream_5xx", "upstream_5xx"),
            media_type="application/json",
        )

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
                    _finish("idle_timeout", error_kind="idle_timeout")
                    return Response(
                        status_code=504,
                        content=error_body("idle_timeout", "idle_timeout"),
                        media_type="application/json",
                    )
                if kind == "expired":
                    metrics.REQUESTS.labels(result="expired").inc()
                    return Response(
                        status_code=401,
                        content='{"error":"expired"}',
                        media_type="application/json",
                    )
                _finish("upstream_timeout", error_kind="timeout")
                return Response(
                    status_code=504,
                    content=error_body("upstream_timeout", "overall_timeout"),
                    media_type="application/json",
                )
            except httpx.HTTPError as exc:
                error, fail_kind, status_code, metric_kind = classify_transport(exc)
                _finish(error, error_kind=metric_kind)
                logger.info("model_proxy upstream error kind=%s job_id=%s", type(exc).__name__, rec.job_id)
                return Response(
                    status_code=status_code,
                    content=error_body(error, fail_kind),
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
        result, _outcome = classify_upstream_status(response.status_code, data)
        metrics.note_model_attempt(result, time.monotonic() - started)
        logger.info(
            "model_proxy outcome job_id=%s kind=%s duration_ms=%s",
            rec.job_id,
            result,
            int((time.monotonic() - started) * 1000),
            extra={"job_id": rec.job_id, "kind": result},
        )
        metrics.ACTIVE_JOBS.set(store.active_job_count())
        return Response(
            content=data,
            status_code=response.status_code,
            headers=out_headers,
            media_type=content_type,
        )
    finally:
        await _close_upstream()
