"""Bound DELETE cancel: background worker + hard wait limit for the HTTP response.

AR-030-004: per-call ``asyncio.run`` can block past the cancel deadline while joining
a stuck DNS resolver executor. The DELETE handler instead submits ``cancel_review_job``
to a long-lived thread pool, waits at most ``LAUNCHER_CANCEL_DEADLINE_SECONDS``, and
returns ``confirmed=false`` immediately if work is still running. Docker cleanup may
continue in the background; lifecycle tombstones are set before wait so duplicate
cancels coalesce; the reaper still reclaims leftovers.

AR-030-005: ``add_done_callback`` must run *outside* ``_INFLIGHT_LOCK``. A non-reentrant
``threading.Lock`` deadlocks when the future is already done and the callback runs
synchronously (``_clear_inflight`` would re-acquire the same lock).

ARB34-002: after the bounded wait returns (confirmed or not), schedule a best-effort
proxy token revoke on the same pool with its own short timeout so a hung model-proxy
cannot delay the DELETE response past the worker's 5s read timeout. Reaper revoke
remains the idempotent backstop.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from app.launcher.job_lifecycle import lifecycle
from app.launcher.timeouts import launcher_cancel_deadline_seconds

logger = logging.getLogger(__name__)

# Long-lived pool — never created per-request (avoids executor shutdown joins).
_CANCEL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opr-cancel")
_INFLIGHT: dict[str, Future] = {}
_INFLIGHT_LOCK = threading.Lock()  # non-reentrant; never call callbacks while held
# Short budget for background cancel-path revoke (ARB34-002); reaper is backstop.
DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS = 2.0


def _cancel_revoke_timeout_seconds() -> float:
    raw = os.environ.get("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS)
    try:
        return max(0.1, float(raw))
    except ValueError:
        return float(DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS)


def schedule_proxy_token_revoke(job_id: str) -> None:
    """Best-effort background revoke; must never block the cancel HTTP response."""

    def _run() -> None:
        try:
            from app.services.model_proxy_client import revoke_job_token_sync

            revoke_job_token_sync(
                job_id=job_id,
                timeout_seconds=_cancel_revoke_timeout_seconds(),
            )
        except Exception:  # noqa: BLE001
            logger.warning("background proxy revoke failed job=%s", job_id, exc_info=True)

    _CANCEL_POOL.submit(_run)


def _clear_inflight(job_id: str, fut: Future) -> None:
    """Drop ``fut`` from the map only if it is still the registered in-flight cancel."""
    with _INFLIGHT_LOCK:
        if _INFLIGHT.get(job_id) is fut:
            del _INFLIGHT[job_id]


def _submit_cancel(job_id: str, *, client: Any | None) -> Future:
    """Submit cancel work, coalescing duplicate in-flight cancels for the same job."""
    from app.launcher.jobs import cancel_review_job

    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(job_id)
        if existing is not None and not existing.done():
            return existing

        def _run() -> dict:
            return cancel_review_job(job_id=job_id, client=client)

        fut = _CANCEL_POOL.submit(_run)
        _INFLIGHT[job_id] = fut
    # Register callback *after* releasing the lock (AR-030-005).
    # If ``fut`` is already done, the callback runs synchronously here — safe.
    fut.add_done_callback(lambda f, jid=job_id: _clear_inflight(jid, f))
    return fut


def cancel_review_job_bounded(*, job_id: str, client: Any | None = None) -> dict:
    """Run cancel in the background; wait at most the launcher cancel deadline.

    Always marks the lifecycle cancel tombstone synchronously before waiting so the
    worker can treat the job as cancelled even when this returns ``confirmed=false``
    while Docker I/O continues.
    """
    from app.launcher.paths import validate_uuid_job_id

    jid = validate_uuid_job_id(job_id)
    # Tombstone immediately (idempotent with cancel_review_job's own request_cancel).
    lifecycle.request_cancel(jid)
    wait_s = float(launcher_cancel_deadline_seconds())
    fut = _submit_cancel(jid, client=client)
    try:
        result = fut.result(timeout=wait_s)
    except FuturesTimeoutError:
        logger.warning(
            "cancel still running after deadline job=%s wait_s=%s; returning unconfirmed",
            jid,
            wait_s,
        )
        result = {"job_id": jid, "removed": 0, "confirmed": False}
    # After the bounded response path — never wait on proxy I/O here (ARB34-002).
    schedule_proxy_token_revoke(jid)
    return result
