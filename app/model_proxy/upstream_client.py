"""Shared upstream HTTP client. A wedged pool must not require a process restart."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class _Use:
    def __init__(self) -> None:
        self.count = 0
        self.retired = False
        self.closing = False
        self.lock = asyncio.Lock()


class UpstreamLease:
    """One in-flight proxy call. A retired client stays open until the last lease ends."""

    def __init__(self, client: httpx.AsyncClient, counted: bool) -> None:
        self.client = client
        self.counted = counted
        self.owned_by_stream = False
        self._released = False

    def hand_to_stream(self) -> None:
        """Keep the client open until the streaming body finishes."""
        self.owned_by_stream = True

    async def release(self) -> None:
        if self._released or not self.counted:
            return
        self._released = True
        await end_upstream_use(self.client)


_uses: dict[int, _Use] = {}
_clients: dict[int, httpx.AsyncClient] = {}


def open_upstream_client() -> httpx.AsyncClient:
    """Open a client that does not reuse sockets.

    A keep-alive connection to the provider can stay half-open. The next review
    then waits on that same socket until the read deadline and returns 504.
    Closing after each response makes the following call dial again.
    """
    client = httpx.AsyncClient(
        http2=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=0),
    )
    _track(client)
    return client


def _track(client: httpx.AsyncClient) -> _Use:
    current = _clients.get(id(client))
    use = _uses.get(id(client))
    if use is None or current is not client:
        use = _Use()
        _uses[id(client)] = use
        _clients[id(client)] = client
    return use


async def open_upstream_lease(client: httpx.AsyncClient) -> UpstreamLease:
    if client.is_closed:
        return UpstreamLease(client, counted=False)
    use = _track(client)
    async with use.lock:
        if use.closing or client.is_closed:
            return UpstreamLease(client, counted=False)
        use.count += 1
    return UpstreamLease(client, counted=True)


async def end_upstream_use(client: httpx.AsyncClient) -> None:
    """Drop one in-flight call and close the client once a retired pool has drained."""
    use = _uses.get(id(client))
    if use is None or _clients.get(id(client)) is not client:
        return
    async with use.lock:
        if use.count > 0:
            use.count -= 1
        should_close = use.retired and use.count == 0 and not use.closing
        if should_close:
            use.closing = True
    if should_close:
        await _aclose(client)


async def retire_upstream_client(app: Any, failed: httpx.AsyncClient, *, reason: str) -> None:
    """Point the app at a new client. Close ``failed`` only when nothing is in flight.

    PoolTimeout means this call found no free slot. Requests already streaming on
    ``failed`` keep their sockets; closing the client here would abort them.
    """
    lock = getattr(app.state, "http_lock", None)
    if lock is None or getattr(app.state, "http", None) is not failed:
        return
    async with lock:
        if getattr(app.state, "http", None) is not failed:
            return
        app.state.http = open_upstream_client()
    logger.warning("model_proxy upstream client reset reason=%s", reason)
    use = _track(failed)
    async with use.lock:
        use.retired = True
        should_close = use.count == 0 and not use.closing
        if should_close:
            use.closing = True
    if should_close:
        await _aclose(failed)


async def close_upstream_clients() -> None:
    """Close every client this process opened or borrowed. Used on shutdown."""
    for client in list(_clients.values()):
        use = _uses.get(id(client))
        if use is not None and _clients.get(id(client)) is client:
            async with use.lock:
                use.retired = True
                use.closing = True
        await _aclose(client)


async def _aclose(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 — shutdown and retirement must both finish
        logger.info("model_proxy upstream client close failed")
    if _clients.get(id(client)) is client:
        _clients.pop(id(client), None)
        _uses.pop(id(client), None)
