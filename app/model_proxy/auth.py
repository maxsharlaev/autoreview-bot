"""Issuer and job-token authentication helpers."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, status

from app.model_proxy.settings import MIN_ISSUER_TOKEN_LEN, ProxySettings
from app.model_proxy.tokens import JobTokenRecord, TokenStore


def require_issuer_auth(
    settings: ProxySettings,
    authorization: str | None,
) -> None:
    expected = settings.issuer_token
    if len(expected) < MIN_ISSUER_TOKEN_LEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="issuer token not configured",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    got = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


def resolve_job_bearer(
    store: TokenStore,
    authorization: str | None,
) -> JobTokenRecord:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    rec = store.resolve(token)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired job token")
    return rec
