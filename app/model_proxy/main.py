"""FastAPI entrypoint for the model-API egress proxy (M3)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.model_proxy import metrics
from app.model_proxy.auth import require_issuer_auth, resolve_job_bearer
from app.model_proxy.providers import build_provider
from app.model_proxy.proxy import allowlist_check, forward_responses, read_bounded_body
from app.model_proxy.settings import ProxySettings, validate_settings
from app.model_proxy.tokens import TokenStore

logger = logging.getLogger(__name__)


class IssueTokenRequest(BaseModel):
    timeout_seconds: int = Field(ge=1, le=86400)
    owner_id: str = Field(default="", max_length=128)


class IssueTokenResponse(BaseModel):
    job_id: str
    token: str
    expires_in: int
    owner_id: str


class RevokeTokenResponse(BaseModel):
    job_id: str
    revoked: bool


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = validate_settings()
    store = TokenStore(settings)
    provider = build_provider(settings)
    client = httpx.AsyncClient(http2=False, follow_redirects=False)
    app.state.settings = settings
    app.state.store = store
    app.state.provider = provider
    app.state.http = client
    logger.info(
        "model_proxy starting provider=%s upstream_host=%s max_review=%s",
        settings.provider,
        settings.upstream_host,
        settings.max_review_timeout_seconds,
    )
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="autoreview-bot model-proxy", version="0.1.0", lifespan=lifespan)


def _settings(request: Request) -> ProxySettings:
    return request.app.state.settings


def _store(request: Request) -> TokenStore:
    return request.app.state.store


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def prometheus_metrics() -> Response:
    payload, content_type = metrics.metrics_payload()
    return Response(content=payload, media_type=content_type)


@app.post("/internal/jobs/{job_id}/token", response_model=IssueTokenResponse)
def issue_token(
    job_id: str,
    body: IssueTokenRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> IssueTokenResponse:
    settings = _settings(request)
    try:
        require_issuer_auth(settings, authorization)
    except HTTPException:
        metrics.AUTH_FAIL.labels(kind="issuer").inc()
        metrics.TOKEN_ISSUE.labels(result="auth_fail").inc()
        raise
    try:
        settings.upstream_key_for_owner(body.owner_id)
    except KeyError as exc:
        metrics.TOKEN_ISSUE.labels(result="no_upstream_key").inc()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        issued = _store(request).issue(
            job_id=job_id,
            timeout_seconds=body.timeout_seconds,
            owner_id=body.owner_id,
        )
    except ValueError as exc:
        metrics.TOKEN_ISSUE.labels(result="invalid").inc()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    metrics.TOKEN_ISSUE.labels(result="ok").inc()
    metrics.ACTIVE_JOBS.set(_store(request).active_job_count())
    return IssueTokenResponse(
        job_id=issued.job_id,
        token=issued.token,
        expires_in=issued.expires_in,
        owner_id=issued.owner_id,
    )


@app.delete("/internal/jobs/{job_id}/token", response_model=RevokeTokenResponse)
def revoke_token(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> RevokeTokenResponse:
    settings = _settings(request)
    try:
        require_issuer_auth(settings, authorization)
    except HTTPException:
        metrics.AUTH_FAIL.labels(kind="issuer").inc()
        metrics.TOKEN_REVOKE.labels(result="auth_fail").inc()
        raise
    revoked = _store(request).revoke(job_id)
    metrics.TOKEN_REVOKE.labels(result="ok" if revoked else "missing").inc()
    metrics.ACTIVE_JOBS.set(_store(request).active_job_count())
    return RevokeTokenResponse(job_id=job_id, revoked=revoked)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def catch_all(full_path: str, request: Request) -> Response:
    """Public surface for review containers: only POST /v1/responses is allowed."""
    deny = allowlist_check(request)
    if deny:
        metrics.ALLOWLIST_DENY.labels(reason=deny).inc()
        metrics.REQUESTS.labels(result="allowlist_deny").inc()
        return Response(status_code=403, content='{"error":"forbidden"}', media_type="application/json")

    settings = _settings(request)
    store = _store(request)
    try:
        rec = resolve_job_bearer(store, request.headers.get("authorization"))
    except HTTPException:
        metrics.AUTH_FAIL.labels(kind="job").inc()
        metrics.REQUESTS.labels(result="auth_fail").inc()
        raise

    cl_raw = request.headers.get("content-length")
    if cl_raw is not None and cl_raw.strip() != "":
        try:
            if int(cl_raw) > settings.max_body_bytes:
                metrics.REQUESTS.labels(result="body_too_large").inc()
                return Response(
                    status_code=413,
                    content='{"error":"body_too_large"}',
                    media_type="application/json",
                )
        except ValueError:
            pass

    limit_err = store.begin_request(rec, body_len=0)
    if limit_err:
        metrics.REQUESTS.labels(result=limit_err).inc()
        code = 429 if limit_err in {"request_limit", "byte_limit", "concurrency_limit"} else 401
        return Response(
            status_code=code,
            content=f'{{"error":"{limit_err}"}}',
            media_type="application/json",
        )

    # ARB32-008: single ownership via finally. Slot stays held until either
    # (a) we transfer to StreamingResponse (stream_body.finally releases), or
    # (b) this finally runs after a non-stream return / error / cancellation.
    slot_held = True
    refund_request = True
    try:
        body, err = await read_bounded_body(
            request,
            settings.max_body_bytes,
            idle_timeout=settings.stream_idle_timeout_seconds,
            overall_timeout=settings.upstream_timeout_seconds,
            rec=rec,
            store=store,
        )
        if err is not None:
            return err
        assert body is not None

        byte_err = store.record_bytes_in(rec, len(body))
        if byte_err:
            metrics.REQUESTS.labels(result=byte_err).inc()
            code = 429 if byte_err == "byte_limit" else 401
            return Response(
                status_code=code,
                content=f'{{"error":"{byte_err}"}}',
                media_type="application/json",
            )

        try:
            upstream_key = settings.upstream_key_for_owner(rec.owner_id)
        except KeyError as exc:
            metrics.REQUESTS.labels(result="no_upstream_key").inc()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="upstream key missing",
            ) from exc

        logger.info(
            "model_proxy forward job_id=%s owner_id=%s body_len=%s",
            rec.job_id,
            rec.owner_id or "-",
            len(body),
        )
        # Committed to upstream: do not refund request count on release.
        refund_request = False
        resp = await forward_responses(
            request=request,
            body=body,
            settings=settings,
            provider=request.app.state.provider,
            store=store,
            rec=rec,
            upstream_api_key=upstream_key,
            client=request.app.state.http,
        )
        # Transfer ownership only after a successful return. StreamingResponse
        # releases in stream_body.finally; non-stream leaves ownership here.
        if isinstance(resp, StreamingResponse):
            slot_held = False
        return resp
    finally:
        if slot_held:
            store.end_request(rec, refund_request=refund_request)
            metrics.ACTIVE_JOBS.set(store.active_job_count())
