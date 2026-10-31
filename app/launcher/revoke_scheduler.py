"""Bounded proxy token revoke scheduler with durable orphan evidence (ARB34-005/009).

Revocations run on a dedicated long-lived asyncio loop with ``asyncio.timeout``
around the async HTTP client. Pending work is bounded; ``schedule_proxy_token_revoke``
returns whether the job was accepted. Same ``job_id`` is deduped.

Transient failures retry with exponential backoff until the revoke is confirmed
or ``LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS`` elapses since the job **creation**
time (container ``opr.created_unix`` / Docker Created, or checkout mtime).

There are **no persistent tombstones**. Restart safety comes from leaving
stopped containers and checkout dirs in place until ``revoke_status`` is
`confirmed` or `window_expired` (config errors retain evidence
until the window); the next reaper pass re-schedules. Stopped
orphans may linger up to the retry window when the proxy is down.
``_CANCEL_POOL`` stays exclusive to Docker cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS = 2.0
DEFAULT_REVOKE_MAX_PENDING = 32
DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS = 600
DEFAULT_TOKEN_GRACE_SECONDS = 60
DEFAULT_RETRY_INITIAL_BACKOFF_SECONDS = 0.5
DEFAULT_RETRY_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_RETRY_SET_MAX = 256
DEFAULT_STATUS_MAX = 512
DEFAULT_CONFIRMED_PRUNE_SECONDS = 600.0
DEFAULT_CONFIG_ERROR_BACKOFF_SECONDS = 60.0


class RevokeJobStatus(StrEnum):
    """Non-blocking per-job revoke status for the reaper (ARB34-009/010)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    WINDOW_EXPIRED = "window_expired"
    CONFIG_ERROR = "config_error"  # 401/403/4xx/missing issuer; keep until window


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


def revoke_retry_window_seconds() -> float:
    """Wall-clock window from job creation for revoke retries / deferred delete."""
    raw_window = os.environ.get("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "").strip()
    if raw_window:
        try:
            return max(1.0, float(raw_window))
        except ValueError:
            pass
    raw_max = os.environ.get("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "").strip()
    raw_grace = os.environ.get("PROXY_TOKEN_GRACE_SECONDS", "").strip()
    try:
        max_review = max(1, int(raw_max)) if raw_max else DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS
    except ValueError:
        max_review = DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS
    try:
        grace = max(0, int(raw_grace)) if raw_grace else DEFAULT_TOKEN_GRACE_SECONDS
    except ValueError:
        grace = DEFAULT_TOKEN_GRACE_SECONDS
    return float(max_review + grace)


def _retry_initial_backoff_seconds() -> float:
    raw = os.environ.get("LAUNCHER_REVOKE_RETRY_INITIAL_BACKOFF_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_RETRY_INITIAL_BACKOFF_SECONDS)
    try:
        return max(0.05, float(raw))
    except ValueError:
        return float(DEFAULT_RETRY_INITIAL_BACKOFF_SECONDS)


def _retry_max_backoff_seconds() -> float:
    raw = os.environ.get("LAUNCHER_REVOKE_RETRY_MAX_BACKOFF_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_RETRY_MAX_BACKOFF_SECONDS)
    try:
        return max(0.1, float(raw))
    except ValueError:
        return float(DEFAULT_RETRY_MAX_BACKOFF_SECONDS)


def _config_error_backoff_seconds() -> float:
    raw = os.environ.get("LAUNCHER_REVOKE_CONFIG_ERROR_BACKOFF_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_CONFIG_ERROR_BACKOFF_SECONDS)
    try:
        return max(5.0, float(raw))
    except ValueError:
        return float(DEFAULT_CONFIG_ERROR_BACKOFF_SECONDS)


def _retry_set_max() -> int:
    raw = os.environ.get("LAUNCHER_REVOKE_RETRY_SET_MAX", "").strip()
    if not raw:
        return int(DEFAULT_RETRY_SET_MAX)
    try:
        return max(1, int(raw))
    except ValueError:
        return int(DEFAULT_RETRY_SET_MAX)


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
        loop = self.ensure_started()
        asyncio.run_coroutine_threadsafe(coro, loop)


_REVOKE_LOOP = _RevokeAsyncLoop()
_PENDING = 0
_PENDING_LOCK = threading.Lock()
_ACTIVE_JOBS: set[str] = set()
_RETRY_NEEDED: dict[str, float] = {}
_STATUS: dict[str, RevokeJobStatus] = {}
_CREATED_AT: dict[str, float] = {}
_CONFIRMED_AT: dict[str, float] = {}


def _dec_pending() -> None:
    global _PENDING
    with _PENDING_LOCK:
        _PENDING = max(0, _PENDING - 1)


def pending_revoke_count() -> int:
    with _PENDING_LOCK:
        return int(_PENDING)


def active_revoke_job_ids() -> set[str]:
    with _PENDING_LOCK:
        return set(_ACTIVE_JOBS)


def retry_needed_job_ids() -> set[str]:
    with _PENDING_LOCK:
        return set(_RETRY_NEEDED)


def _clear_active(job_id: str) -> None:
    with _PENDING_LOCK:
        _ACTIVE_JOBS.discard(job_id)


def _remember_created(job_id: str, created_at: float | None) -> float | None:
    if created_at is None:
        return _CREATED_AT.get(job_id)
    cat = float(created_at)
    prev = _CREATED_AT.get(job_id)
    if prev is None or cat < prev:
        _CREATED_AT[job_id] = cat
        return cat
    return prev


def _prune_status_locked(now_mono: float) -> None:
    stale = [jid for jid, when in _CONFIRMED_AT.items() if (now_mono - when) >= DEFAULT_CONFIRMED_PRUNE_SECONDS]
    for jid in stale:
        _CONFIRMED_AT.pop(jid, None)
        if _STATUS.get(jid) is RevokeJobStatus.CONFIRMED:
            _STATUS.pop(jid, None)
            _CREATED_AT.pop(jid, None)
    while len(_STATUS) > DEFAULT_STATUS_MAX:
        confirmed = [j for j, s in _STATUS.items() if s is RevokeJobStatus.CONFIRMED]
        if confirmed:
            victim = confirmed[0]
        elif _CREATED_AT:
            victim = min(_CREATED_AT, key=_CREATED_AT.get)  # type: ignore[arg-type]
        else:
            victim = next(iter(_STATUS))
        _STATUS.pop(victim, None)
        _CONFIRMED_AT.pop(victim, None)
        _CREATED_AT.pop(victim, None)


def _set_status(job_id: str, status: RevokeJobStatus) -> None:
    with _PENDING_LOCK:
        _STATUS[job_id] = status
        if status is RevokeJobStatus.CONFIRMED:
            _CONFIRMED_AT[job_id] = time.monotonic()
        _prune_status_locked(time.monotonic())


def revoke_status(
    job_id: str,
    *,
    created_at: float | None = None,
    now: float | None = None,
) -> RevokeJobStatus:
    """Non-blocking revoke status for ``job_id`` (reaper polls each pass)."""
    jid = (job_id or "").strip()
    if not jid:
        return RevokeJobStatus.CONFIRMED
    clock = time.time() if now is None else float(now)
    window = revoke_retry_window_seconds()
    with _PENDING_LOCK:
        if created_at is not None:
            _remember_created(jid, created_at)
        cat = _CREATED_AT.get(jid, created_at)
        st = _STATUS.get(jid)
        if st is RevokeJobStatus.CONFIRMED:
            return RevokeJobStatus.CONFIRMED
        if cat is not None and clock >= float(cat) + window:
            _STATUS[jid] = RevokeJobStatus.WINDOW_EXPIRED
            return RevokeJobStatus.WINDOW_EXPIRED
        if st is RevokeJobStatus.WINDOW_EXPIRED:
            return RevokeJobStatus.WINDOW_EXPIRED
        if st is RevokeJobStatus.CONFIG_ERROR:
            return RevokeJobStatus.CONFIG_ERROR
        if st is RevokeJobStatus.PENDING or jid in _ACTIVE_JOBS:
            return RevokeJobStatus.PENDING
        return RevokeJobStatus.PENDING


def may_delete_after_revoke(
    job_id: str,
    *,
    created_at: float | None = None,
    now: float | None = None,
) -> bool:
    """True when reaper may remove container/checkout for ``job_id``."""
    st = revoke_status(job_id, created_at=created_at, now=now)
    return st in (RevokeJobStatus.CONFIRMED, RevokeJobStatus.WINDOW_EXPIRED)


def note_revoke_needed(job_id: str, *, deadline: float | None = None) -> None:
    """In-memory schedule hint; durable signal is the stopped orphan itself."""
    jid = (job_id or "").strip()
    if not jid:
        return
    now = time.time()
    until = float(deadline) if deadline is not None else now + revoke_retry_window_seconds()
    with _PENDING_LOCK:
        expired = [k for k, d in _RETRY_NEEDED.items() if d <= now]
        for k in expired:
            _RETRY_NEEDED.pop(k, None)
        if jid in _RETRY_NEEDED:
            _RETRY_NEEDED[jid] = max(_RETRY_NEEDED[jid], until)
            return
        if len(_RETRY_NEEDED) >= _retry_set_max():
            victim = min(_RETRY_NEEDED, key=_RETRY_NEEDED.get)  # type: ignore[arg-type]
            _RETRY_NEEDED.pop(victim, None)
            logger.warning(
                "revoke retry set full max=%s; evicting job=%s for job=%s",
                _retry_set_max(),
                victim,
                jid,
            )
        _RETRY_NEEDED[jid] = until


def clear_revoke_needed(job_id: str) -> None:
    with _PENDING_LOCK:
        _RETRY_NEEDED.pop((job_id or "").strip(), None)


def drain_revoke_retries() -> int:
    now = time.time()
    with _PENDING_LOCK:
        items = list(_RETRY_NEEDED.items())
        created = dict(_CREATED_AT)
    accepted = 0
    for jid, deadline in items:
        if deadline <= now:
            clear_revoke_needed(jid)
            continue
        if schedule_proxy_token_revoke(jid, created_at=created.get(jid)):
            clear_revoke_needed(jid)
            accepted += 1
    return accepted


async def _revoke_with_retries(
    job_id: str,
    budget: float,
    retry_deadline: float,
) -> None:
    from app.services.model_proxy_client import RevokeAttemptOutcome, revoke_job_token_attempt

    backoff = _retry_initial_backoff_seconds()
    max_backoff = _retry_max_backoff_seconds()
    try:
        while True:
            outcome = RevokeAttemptOutcome.TRANSIENT
            try:
                async with asyncio.timeout(budget):
                    outcome = await revoke_job_token_attempt(job_id=job_id, timeout_seconds=budget)
            except TimeoutError:
                logger.warning(
                    "background proxy revoke timed out job=%s budget_s=%s",
                    job_id,
                    budget,
                )
                outcome = RevokeAttemptOutcome.TRANSIENT
            except Exception:  # noqa: BLE001
                logger.warning("background proxy revoke failed job=%s", job_id, exc_info=True)
                outcome = RevokeAttemptOutcome.TRANSIENT

            if outcome is RevokeAttemptOutcome.DONE:
                clear_revoke_needed(job_id)
                _set_status(job_id, RevokeJobStatus.CONFIRMED)
                return

            now = time.time()
            if now >= retry_deadline:
                logger.warning(
                    "background proxy revoke giving up job=%s after retry window",
                    job_id,
                )
                clear_revoke_needed(job_id)
                _set_status(job_id, RevokeJobStatus.WINDOW_EXPIRED)
                return

            if outcome is RevokeAttemptOutcome.CONFIG_ERROR:
                _set_status(job_id, RevokeJobStatus.CONFIG_ERROR)
                logger.error(
                    "proxy revoke config error job=%s; retaining evidence, low-frequency retry until window",
                    job_id,
                )
                sleep_for = min(
                    _config_error_backoff_seconds(),
                    max(0.0, retry_deadline - now),
                )
            else:
                sleep_for = min(backoff, max(0.0, retry_deadline - now))
                backoff = min(max_backoff, backoff * 2.0)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        _clear_active(job_id)
        _dec_pending()


def schedule_proxy_token_revoke(
    job_id: str,
    *,
    created_at: float | None = None,
) -> bool:
    """Best-effort background revoke; never blocks cancel/reaper.

    Returns True if accepted (queued, in-flight, already confirmed, or window
    already expired). Returns False if the pending queue is saturated.
    ``created_at`` is the job creation unix time used for the retry window.
    """
    global _PENDING
    jid = (job_id or "").strip()
    if not jid:
        return True
    budget = _cancel_revoke_timeout_seconds()
    max_pending = _revoke_max_pending()
    window = revoke_retry_window_seconds()
    now = time.time()
    with _PENDING_LOCK:
        cat = _remember_created(jid, created_at)
        if cat is None:
            cat = now
            _CREATED_AT[jid] = cat
        if _STATUS.get(jid) is RevokeJobStatus.CONFIRMED:
            return True
        if now >= float(cat) + window:
            _STATUS[jid] = RevokeJobStatus.WINDOW_EXPIRED
            return True
        if jid in _ACTIVE_JOBS:
            _STATUS[jid] = RevokeJobStatus.PENDING
            return True
        if _PENDING >= max_pending:
            logger.warning(
                "proxy revoke queue saturated pending=%s max=%s; rejecting job=%s "
                "(keep stopped orphan; reaper will retry)",
                _PENDING,
                max_pending,
                jid,
            )
            return False
        _PENDING += 1
        _ACTIVE_JOBS.add(jid)
        _STATUS[jid] = RevokeJobStatus.PENDING
        retry_deadline = float(cat) + window
    try:
        _REVOKE_LOOP.submit(_revoke_with_retries(jid, budget, retry_deadline))
    except Exception:  # noqa: BLE001
        _clear_active(jid)
        _dec_pending()
        logger.warning("background proxy revoke submit failed job=%s", jid, exc_info=True)
        return False
    return True


def _reset_for_tests() -> None:
    """Clear in-memory state (tests use this to simulate launcher restart)."""
    global _PENDING
    with _PENDING_LOCK:
        _PENDING = 0
        _ACTIVE_JOBS.clear()
        _RETRY_NEEDED.clear()
        _STATUS.clear()
        _CREATED_AT.clear()
        _CONFIRMED_AT.clear()
