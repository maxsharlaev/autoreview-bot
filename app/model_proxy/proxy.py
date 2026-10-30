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

# Representation metadata must not be forwarded after we (or HTTPX) may have
# decoded / re-chunked the body. Always strip these from the client response.
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


async def read_bounded_body(request: Request, max_bytes: int) -> tuple[bytes | None, Response | None]:
    """Read the request body with a hard size bound.

    Rejects oversized ``Content-Length`` before reading. For chunked / unknown
    length, reads ``request.stream()`` incrementally and aborts at ``max_bytes``.
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

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
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


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


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
    reason = allowlist_check(request)
    if reason:
        metrics.ALLOWLIST_DENY.labels(reason=reason).inc()
        metrics.REQUESTS.labels(result="allowlist_deny").inc()
        return Response(status_code=403, content='{"error":"forbidden"}', media_type="application/json")

    if len(body) > settings.max_body_bytes:
        metrics.REQUESTS.labels(result="body_too_large").inc()
        return Response(status_code=413, content='{"error":"body_too_large"}', media_type="application/json")

    limit_err = store.begin_request(rec, body_len=len(body))
    if limit_err:
        metrics.REQUESTS.labels(result=limit_err).inc()
        code = 429 if limit_err in {"request_limit", "byte_limit", "concurrency_limit"} else 401
        return Response(
            status_code=code,
            content=f'{{"error":"{limit_err}"}}',
            media_type="application/json",
        )

    target = provider.responses_target()
    headers = {
        "Authorization": f"Bearer {upstream_api_key}",
        "Content-Type": request.headers.get("content-type") or "application/json",
        "Accept": request.headers.get("accept") or "application/json",
        # Prefer identity so the forwarded body matches stripped representation headers.
        # Also disables HTTPX auto-decompression when this header is present.
        "Accept-Encoding": "identity",
    }
    for name in ("openai-beta", "user-agent", "originator", "x-codex-beta-features"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    overall_deadline = time.monotonic() + float(settings.upstream_timeout_seconds)
    idle = float(settings.stream_idle_timeout_seconds)
    # HTTPX float timeouts are per-phase inactivity only (lesson from #30); they are a
    # soft backstop. Authoritative bounds use asyncio.timeout / wall-clock below.
    httpx_timeout = httpx.Timeout(
        connect=min(10.0, float(settings.upstream_timeout_seconds)),
        read=max(idle, 1.0),
        write=60.0,
        pool=10.0,
    )

    response: httpx.Response | None = None
    try:
        if not store.still_valid(rec):
            store.end_request(rec)
            metrics.REQUESTS.labels(result="expired").inc()
            return Response(status_code=401, content='{"error":"expired"}', media_type="application/json")

        upstream = client.build_request(
            "POST",
            target.url,
            headers=headers,
            content=body,
            timeout=httpx_timeout,
        )
        budget = _remaining(overall_deadline)
        if budget <= 0:
            store.end_request(rec)
            metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
            metrics.REQUESTS.labels(result="upstream_timeout").inc()
            return Response(
                status_code=504,
                content='{"error":"upstream_timeout"}',
                media_type="application/json",
            )
        try:
            async with asyncio.timeout(budget):
                response = await client.send(upstream, stream=True)
        except TimeoutError:
            store.end_request(rec)
            metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
            metrics.REQUESTS.labels(result="upstream_timeout").inc()
            return Response(
                status_code=504,
                content='{"error":"upstream_timeout"}',
                media_type="application/json",
            )
    except httpx.TimeoutException:
        store.end_request(rec)
        metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
        metrics.REQUESTS.labels(result="upstream_timeout").inc()
        return Response(status_code=504, content='{"error":"upstream_timeout"}', media_type="application/json")
    except httpx.HTTPError as exc:
        store.end_request(rec)
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
        except Exception:  # noqa: BLE001 - best-effort close on cap/timeout
            pass

    async def _next_chunk(aiter: AsyncIterator[bytes]) -> bytes | None:
        """Wait for the next upstream chunk under idle + overall wall-clock bounds.

        Returns ``None`` on clean EOF or token expiry (upstream closed). Raises
        ``TimeoutError("idle"|"overall")`` when a deadline fires.
        """
        if not store.still_valid(rec):
            await _close_upstream()
            return None
        remaining = _remaining(overall_deadline)
        if remaining <= 0:
            await _close_upstream()
            raise TimeoutError("overall")
        wait = min(idle, remaining)
        if wait <= 0:
            await _close_upstream()
            raise TimeoutError("overall")
        try:
            async with asyncio.timeout(wait):
                return await anext(aiter)
        except StopAsyncIteration:
            return None
        except TimeoutError as exc:
            await _close_upstream()
            if _remaining(overall_deadline) <= 0 or wait < idle:
                raise TimeoutError("overall") from exc
            raise TimeoutError("idle") from exc

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
        return StreamingResponse(
            stream_body(),
            status_code=response.status_code,
            headers=out_headers,
            media_type=content_type,
        )

    # Non-stream: read incrementally under the same wall-clock / byte / token bounds.
    # Never unbounded aread().
    chunks: list[bytes] = []
    aiter = response.aiter_bytes()
    ended = False
    try:
        while True:
            try:
                chunk = await _next_chunk(aiter)
            except TimeoutError as exc:
                store.end_request(rec)
                ended = True
                kind = str(exc) or "timeout"
                if kind == "idle":
                    metrics.UPSTREAM_ERRORS.labels(kind="idle_timeout").inc()
                    metrics.REQUESTS.labels(result="idle_timeout").inc()
                    return Response(
                        status_code=504,
                        content='{"error":"idle_timeout"}',
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
                if not store.still_valid(rec) and not chunks:
                    store.end_request(rec)
                    ended = True
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
                store.end_request(rec)
                ended = True
                metrics.REQUESTS.labels(result="byte_limit").inc()
                return Response(
                    status_code=429,
                    content='{"error":"byte_limit"}',
                    media_type="application/json",
                )
            chunks.append(chunk)
            metrics.BYTES.labels(direction="out").inc(accepted)

        data = b"".join(chunks)
        store.end_request(rec)
        ended = True
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
        if not ended:
            store.end_request(rec)
            metrics.ACTIVE_JOBS.set(store.active_job_count())
