"""HTTP client for model-proxy issuer API (worker + launcher reaper)."""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class RevokeAttemptOutcome(StrEnum):
    """Classify one proxy revoke HTTP attempt for scheduler retry (ARB34-009)."""

    DONE = "done"  # revoked, already missing, or non-retryable client/auth error
    TRANSIENT = "transient"  # timeout, connection error, or 5xx


DEFAULT_MODEL_PROXY_URL = "http://model-proxy:8081"


def model_proxy_url() -> str:
    return (os.environ.get("MODEL_PROXY_URL") or DEFAULT_MODEL_PROXY_URL).rstrip("/")


def proxy_issuer_token() -> str:
    return (os.environ.get("PROXY_ISSUER_TOKEN") or "").strip()


def issuer_configured() -> bool:
    return len(proxy_issuer_token()) >= 16


class ModelProxyIssuerError(RuntimeError):
    """Issuer call failed (auth, missing key, network, or bad response)."""


async def issue_job_token(
    *,
    job_id: str,
    timeout_seconds: int,
    owner_id: str = "",
    base_url: str | None = None,
    issuer_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """POST /internal/jobs/{job_id}/token. Returns the raw ``opr_job_`` token."""
    token = (issuer_token if issuer_token is not None else proxy_issuer_token()).strip()
    if len(token) < 16:
        raise ModelProxyIssuerError("PROXY_ISSUER_TOKEN missing or too short")
    url = f"{(base_url or model_proxy_url()).rstrip('/')}/internal/jobs/{job_id}/token"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body: dict[str, Any] = {"timeout_seconds": int(timeout_seconds), "owner_id": owner_id or ""}
    owns = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0))
    try:
        response = await http.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ModelProxyIssuerError(f"model-proxy issue unreachable: {exc}") from exc
    finally:
        if owns:
            await http.aclose()
    if response.status_code == 401:
        raise ModelProxyIssuerError("model-proxy issuer auth failed")
    if response.status_code >= 400:
        raise ModelProxyIssuerError(f"model-proxy issue HTTP {response.status_code}: {response.text[:200]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ModelProxyIssuerError("model-proxy issue returned non-JSON") from exc
    raw = data.get("token") if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw.startswith("opr_job_"):
        raise ModelProxyIssuerError("model-proxy issue returned invalid token")
    return raw


async def revoke_job_token(
    *,
    job_id: str,
    base_url: str | None = None,
    issuer_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float | None = None,
) -> bool:
    """DELETE /internal/jobs/{job_id}/token. Idempotent: True if revoked, False if missing/unreachable.

    `timeout_seconds` caps the HTTP client phases when this call owns the client.
    Callers that pass a shared `client` should bound the await with `asyncio.timeout`.
    """
    token = (issuer_token if issuer_token is not None else proxy_issuer_token()).strip()
    if len(token) < 16:
        logger.warning("proxy revoke skipped job_id=%s: PROXY_ISSUER_TOKEN not configured", job_id)
        return False
    url = f"{(base_url or model_proxy_url()).rstrip('/')}/internal/jobs/{job_id}/token"
    headers = {"Authorization": f"Bearer {token}"}
    owns = client is None
    if timeout_seconds is None:
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    else:
        budget = max(0.1, float(timeout_seconds))
        timeout = httpx.Timeout(connect=budget, read=budget, write=budget, pool=budget)
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await http.delete(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("model-proxy revoke unreachable job_id=%s: %s", job_id, exc)
        return False
    finally:
        if owns:
            await http.aclose()
    if response.status_code == 401:
        logger.warning("model-proxy revoke auth failed job_id=%s", job_id)
        return False
    if response.status_code not in (200, 204):
        logger.warning(
            "model-proxy revoke HTTP %s job_id=%s body=%s",
            response.status_code,
            job_id,
            response.text[:200],
        )
        return False
    try:
        data = response.json()
    except ValueError:
        return response.status_code in (200, 204)
    if isinstance(data, dict) and "revoked" in data:
        return bool(data.get("revoked"))
    return True


async def revoke_job_token_attempt(
    *,
    job_id: str,
    base_url: str | None = None,
    issuer_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float | None = None,
) -> RevokeAttemptOutcome:
    """One revoke attempt with DONE vs TRANSIENT classification (ARB34-009).

    DONE covers success, already-missing (200 revoked=false / 404), and
    non-retryable 4xx/auth/config gaps. TRANSIENT covers network errors and 5xx.
    """
    token = (issuer_token if issuer_token is not None else proxy_issuer_token()).strip()
    if len(token) < 16:
        logger.warning("proxy revoke skipped job_id=%s: PROXY_ISSUER_TOKEN not configured", job_id)
        return RevokeAttemptOutcome.DONE
    url = f"{(base_url or model_proxy_url()).rstrip('/')}/internal/jobs/{job_id}/token"
    headers = {"Authorization": f"Bearer {token}"}
    owns = client is None
    if timeout_seconds is None:
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    else:
        budget = max(0.1, float(timeout_seconds))
        timeout = httpx.Timeout(connect=budget, read=budget, write=budget, pool=budget)
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        try:
            response = await http.delete(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("model-proxy revoke unreachable job_id=%s: %s", job_id, exc)
            return RevokeAttemptOutcome.TRANSIENT
    finally:
        if owns:
            await http.aclose()
    if response.status_code == 401:
        logger.warning("model-proxy revoke auth failed job_id=%s", job_id)
        return RevokeAttemptOutcome.DONE
    if response.status_code == 404:
        return RevokeAttemptOutcome.DONE
    if response.status_code in (200, 204):
        # revoked true/false both mean the token is gone or never existed.
        return RevokeAttemptOutcome.DONE
    if response.status_code >= 500:
        logger.warning(
            "model-proxy revoke HTTP %s job_id=%s body=%s",
            response.status_code,
            job_id,
            response.text[:200],
        )
        return RevokeAttemptOutcome.TRANSIENT
    # Other 4xx: not retryable
    logger.warning(
        "model-proxy revoke HTTP %s job_id=%s body=%s",
        response.status_code,
        job_id,
        response.text[:200],
    )
    return RevokeAttemptOutcome.DONE


def revoke_job_token_sync(
    *,
    job_id: str,
    base_url: str | None = None,
    issuer_token: str | None = None,
    timeout_seconds: float | None = None,
) -> bool:
    """Sync revoke for launcher reaper / background cancel (thread context). Idempotent.

    ``timeout_seconds`` caps connect/read/write/pool (default 5/10/10/5). Cancel-path
    background revoke passes a short budget so a hung proxy cannot stall the pool.
    """
    token = (issuer_token if issuer_token is not None else proxy_issuer_token()).strip()
    if len(token) < 16:
        logger.warning("proxy revoke skipped job_id=%s: PROXY_ISSUER_TOKEN not configured", job_id)
        return False
    url = f"{(base_url or model_proxy_url()).rstrip('/')}/internal/jobs/{job_id}/token"
    headers = {"Authorization": f"Bearer {token}"}
    if timeout_seconds is None:
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    else:
        budget = max(0.1, float(timeout_seconds))
        timeout = httpx.Timeout(connect=budget, read=budget, write=budget, pool=budget)
    try:
        with httpx.Client(timeout=timeout) as http:
            response = http.delete(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("model-proxy revoke unreachable job_id=%s: %s", job_id, exc)
        return False
    if response.status_code == 401:
        logger.warning("model-proxy revoke auth failed job_id=%s", job_id)
        return False
    if response.status_code not in (200, 204):
        logger.warning(
            "model-proxy revoke HTTP %s job_id=%s body=%s",
            response.status_code,
            job_id,
            response.text[:200],
        )
        return False
    try:
        data = response.json()
    except ValueError:
        return True
    if isinstance(data, dict) and "revoked" in data:
        return bool(data.get("revoked"))
    return True
