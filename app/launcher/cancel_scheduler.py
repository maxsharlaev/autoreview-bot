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

ARB34-002 / ARB34-005: after the bounded wait returns (confirmed or not), schedule a
best-effort proxy token revoke on the *separate* revoke scheduler (dedicated asyncio
loop + wall-clock ``asyncio.timeout``, bounded pending queue). ``_CANCEL_POOL`` is
exclusive to Docker cancellation so blocked revokes cannot starve cancels. Reaper
stopped containers remain for the reaper until revoke confirms or the retry window elapses.

ARB34-015: cancel passes ``expires_at`` from job.json / ``opr.token_expires_unix`` when
known so a short attempt bound cannot mark ``WINDOW_EXPIRED`` and drop evidence early.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from app.launcher.job_lifecycle import lifecycle
from app.launcher.revoke_scheduler import note_revoke_needed, schedule_proxy_token_revoke
from app.launcher.timeouts import launcher_cancel_deadline_seconds

logger = logging.getLogger(__name__)

# Long-lived pool — Docker cancel only (ARB34-005); never schedule proxy revoke here.
_CANCEL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opr-cancel")
_INFLIGHT: dict[str, Future] = {}
_INFLIGHT_LOCK = threading.Lock()  # non-reentrant; never call callbacks while held


def _lookup_token_expires_unix(job_id: str, *, client: Any | None = None) -> float | None:
    """Best-effort absolute token expiry for cancel-path revoke (ARB34-015).

    Prefers staged ``job.json`` ``token_expires_unix``, then the review container
    label ``opr.token_expires_unix``. Never blocks cancel on lookup failure.
    """
    from app.launcher.reaper import (
        checkout_base_path,
        job_token_expires_unix_from_checkout,
        job_token_expires_unix_from_container,
    )

    try:
        path = checkout_base_path() / job_id
        exp = job_token_expires_unix_from_checkout(path)
        if exp is not None:
            return float(exp)
    except Exception:  # noqa: BLE001 - cancel must not fail on lookup
        logger.debug("cancel expires_at checkout lookup failed job=%s", job_id, exc_info=True)

    owns = client is None
    docker = client
    try:
        if docker is None:
            from app.launcher.docker_api import DockerClient

            docker = DockerClient()
        listed = docker.list_containers_for_job(job_id, timeout=0.5)
        for item in listed or []:
            exp = job_token_expires_unix_from_container(item)
            if exp is not None:
                return float(exp)
    except Exception:  # noqa: BLE001
        logger.debug("cancel expires_at container lookup failed job=%s", job_id, exc_info=True)
    finally:
        if owns and docker is not None:
            try:
                docker.close()
            except Exception:  # noqa: BLE001
                pass
    return None


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
    # After the bounded response path — never wait on proxy I/O here (ARB34-002/005).
    # Pass known expires_at so attempt bounds cannot mark WINDOW_EXPIRED (ARB34-015).
    expires_at = _lookup_token_expires_unix(jid, client=client)
    if not schedule_proxy_token_revoke(jid, expires_at=expires_at):
        note_revoke_needed(jid)
    return result
