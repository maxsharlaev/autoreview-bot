"""Shared upstream HTTP client. A wedged pool must not require a process restart."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def open_upstream_client() -> httpx.AsyncClient:
    """Open a client that does not reuse sockets.

    A keep-alive connection to the provider can stay half-open. The next review
    then waits on that same socket until the read deadline and returns 504.
    Closing after each response makes the following call dial again.
    """
    return httpx.AsyncClient(
        http2=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=0),
    )


async def retire_upstream_client(app: Any, failed: httpx.AsyncClient, *, reason: str) -> None:
    """Replace ``failed`` if it is still the app client, then close it.

    PoolTimeout means every slot is stuck. Closing that client drops them so the
    next request can connect. In-flight calls on the old client fail with it.
    """
    lock = getattr(app.state, "http_lock", None)
    if lock is None or getattr(app.state, "http", None) is not failed:
        return
    async with lock:
        if getattr(app.state, "http", None) is not failed:
            return
        app.state.http = open_upstream_client()
    logger.warning("model_proxy upstream client reset reason=%s", reason)
    try:
        await failed.aclose()
    except Exception:  # noqa: BLE001 — the replacement is already installed
        logger.info("model_proxy upstream client close failed")
