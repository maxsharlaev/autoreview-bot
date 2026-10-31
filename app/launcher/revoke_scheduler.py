"""Best-effort proxy token revoke off the Docker cancel pool (ARB34-005).

Revocations run on a dedicated long-lived asyncio loop with ``asyncio.timeout``
around the async HTTP client (same wall-clock pattern as AR-030 / #30). Pending
work is bounded; when saturated the schedule is dropped with a log line and the
reaper remains the idempotent backstop. ``_CANCEL_POOL`` stays exclusive to
Docker cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS = 2.0
DEFAULT_REVOKE_MAX_PENDING = 32


def _cancel_revoke_timeout_seconds() -> float:
    raw = os.environ.get("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS)
    try:
        return max(0.1, float(raw))
    except ValueError:
        return float(DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS)


def _revoke_max_pending() -> int:
    raw = os.environ.get("LAUNCHER_CANCEL_REVOKE_MAX_PENDING", "").strip()
    if not raw:
        return int(DEFAULT_REVOKE_MAX_PENDING)
    try:
        return max(1, int(raw))
    except ValueError:
        return int(DEFAULT_REVOKE_MAX_PENDING)


class _RevokeAsyncLoop:
    """Dedicated daemon thread + event loop for proxy revoke HTTP."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None:
                return self._loop

            def _thread_main() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._ready.set()
                loop.run_forever()

            self._thread = threading.Thread(
                target=_thread_main,
                name="opr-proxy-revoke-async",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("proxy revoke async loop failed to start")
        assert self._loop is not None
        return self._loop

    def submit(self, coro: Any) -> None:
        """Fire-and-forget on the revoke loop (errors logged inside the coro)."""
        loop = self.ensure_started()
        asyncio.run_coroutine_threadsafe(coro, loop)


_REVOKE_LOOP = _RevokeAsyncLoop()
_PENDING = 0
_PENDING_LOCK = threading.Lock()


def _dec_pending() -> None:
    global _PENDING
    with _PENDING_LOCK:
        _PENDING = max(0, _PENDING - 1)


def pending_revoke_count() -> int:
    """Test helper: in-flight + queued revoke coroutines that hold a slot."""
    with _PENDING_LOCK:
        return int(_PENDING)


async def _revoke_with_deadline(job_id: str, budget: float) -> None:
    from app.services.model_proxy_client import revoke_job_token

    try:
        async with asyncio.timeout(budget):
            await revoke_job_token(job_id=job_id, timeout_seconds=budget)
    except TimeoutError:
        logger.warning(
            "background proxy revoke timed out job=%s budget_s=%s",
            job_id,
            budget,
        )
    except Exception:  # noqa: BLE001
        logger.warning("background proxy revoke failed job=%s", job_id, exc_info=True)
    finally:
        _dec_pending()


def schedule_proxy_token_revoke(job_id: str) -> None:
    """Best-effort background revoke; never blocks the cancel HTTP response.

    If the pending queue is saturated, drops the schedule (reaper backstop).
    """
    global _PENDING
    budget = _cancel_revoke_timeout_seconds()
    max_pending = _revoke_max_pending()
    with _PENDING_LOCK:
        if _PENDING >= max_pending:
            logger.warning(
                "proxy revoke queue saturated pending=%s max=%s; dropping job=%s (reaper backstop)",
                _PENDING,
                max_pending,
                job_id,
            )
            return
        _PENDING += 1
    try:
        _REVOKE_LOOP.submit(_revoke_with_deadline(job_id, budget))
    except Exception:  # noqa: BLE001
        _dec_pending()
        logger.warning("background proxy revoke submit failed job=%s", job_id, exc_info=True)
