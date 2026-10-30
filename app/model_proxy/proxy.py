"""Allowlisted reverse proxy for POST /v1/responses with SSE passthrough."""

from __future__ import annotations

import logging
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


def allowlist_check(request: Request) -> str | None:
    """Return deny reason or None if allowed."""
    if request.method.upper() == "CONNECT":
        return "connect"
    if request.headers.get("upgrade", "").lower() == "websocket":
        return "websocket"
    path = request.url.path
    # Reject absolute-form URLs and anything except /v1/responses
    if path.startswith("http://") or path.startswith("https://"):
        return "absolute_url"
    if request.method.upper() != "POST":
        return "method"
    if path.rstrip("/") != "/v1/responses":
        return "path"
    return None


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
    }
    # Forward a small safe subset of client headers (never Authorization from client).
    for name in ("openai-beta", "user-agent", "originator", "x-codex-beta-features"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    bytes_out = 0
    try:
        upstream = client.build_request(
            "POST",
            target.url,
            headers=headers,
            content=body,
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.upstream_timeout_seconds,
                write=60.0,
                pool=10.0,
            ),
        )
        response = await client.send(upstream, stream=True)
    except httpx.TimeoutException:
        store.end_request(rec, bytes_out=0)
        metrics.UPSTREAM_ERRORS.labels(kind="timeout").inc()
        metrics.REQUESTS.labels(result="upstream_timeout").inc()
        return Response(status_code=504, content='{"error":"upstream_timeout"}', media_type="application/json")
    except httpx.HTTPError as exc:
        store.end_request(rec, bytes_out=0)
        metrics.UPSTREAM_ERRORS.labels(kind="http_error").inc()
        metrics.REQUESTS.labels(result="upstream_error").inc()
        logger.warning("model_proxy upstream error job_id=%s: %s", rec.job_id, type(exc).__name__)
        return Response(status_code=502, content='{"error":"upstream_error"}', media_type="application/json")

    metrics.BYTES.labels(direction="in").inc(len(body))
    content_type = response.headers.get("content-type", "application/json")
    is_stream = "text/event-stream" in content_type.lower()

    out_headers = {}
    for key, value in response.headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        if key.lower() == "authorization":
            continue
        out_headers[key] = value

    async def stream_body() -> AsyncIterator[bytes]:
        nonlocal bytes_out
        try:
            idle = settings.stream_idle_timeout_seconds
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                bytes_out += len(chunk)
                metrics.BYTES.labels(direction="out").inc(len(chunk))
                yield chunk
                # Soft idle note: httpx read timeout already bounds stalls.
                _ = idle
        finally:
            await response.aclose()
            store.end_request(rec, bytes_out=bytes_out)
            metrics.REQUESTS.labels(result="ok" if response.status_code < 400 else "upstream_status").inc()
            metrics.ACTIVE_JOBS.set(store.active_job_count())

    if is_stream or response.headers.get("transfer-encoding", "").lower() == "chunked":
        return StreamingResponse(
            stream_body(),
            status_code=response.status_code,
            headers=out_headers,
            media_type=content_type,
        )

    # Non-stream: buffer (still within body limits for response via max bytes per job)
    try:
        data = await response.aread()
        bytes_out = len(data)
        metrics.BYTES.labels(direction="out").inc(len(data))
        metrics.REQUESTS.labels(result="ok" if response.status_code < 400 else "upstream_status").inc()
        return Response(content=data, status_code=response.status_code, headers=out_headers, media_type=content_type)
    finally:
        await response.aclose()
        store.end_request(rec, bytes_out=bytes_out)
        metrics.ACTIVE_JOBS.set(store.active_job_count())
