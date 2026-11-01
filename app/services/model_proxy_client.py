"""HTTP client for model-proxy issuer API (worker)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

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
        raise ModelProxyIssuerError("model-proxy issue unauthorized (issuer token)")
    if response.status_code >= 400:
        raise ModelProxyIssuerError(f"model-proxy issue failed: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ModelProxyIssuerError("model-proxy issue returned non-JSON") from exc
    raw = str((data or {}).get("token") or "")
    if not raw.startswith("opr_job_"):
        raise ModelProxyIssuerError("model-proxy issue returned unexpected token")
    return raw
