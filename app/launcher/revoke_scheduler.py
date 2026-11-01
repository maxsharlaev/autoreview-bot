"""Bounded proxy token revoke scheduler with durable orphan evidence (ARB34-005/009).

Revocations run on a dedicated long-lived asyncio loop with ``asyncio.timeout``
around the async HTTP client. Pending work is bounded; ``schedule_proxy_token_revoke``
returns whether the job was accepted. Same ``job_id`` is deduped.

Transient failures retry with exponential backoff until the revoke is confirmed
or the proxy token ``expires_at`` (+ clock-skew margin) elapses (ARB34-014).
``expires_at`` comes from model-proxy issuance (label/job.json); the scheduler keeps
the **latest** known deadline. Without ``expires_at``, fall back to issuance-receipt
+ full TTL when known; otherwise retain until the orphan TTL gate.
Derived TTL defaults to ``OPR_MAX_REVIEW_TIMEOUT_SECONDS + PROXY_TOKEN_GRACE_SECONDS``
(ARB34-013); an explicit ``LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS`` may only raise it.

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
DEFAULT_CLOCK_SKEW_SECONDS = 30.0


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


def proxy_token_grace_seconds() -> int:
    """``PROXY_TOKEN_GRACE_SECONDS`` (shared with model-proxy token TTL grace)."""
    raw = os.environ.get("PROXY_TOKEN_GRACE_SECONDS", "").strip()
    if not raw:
        return int(DEFAULT_TOKEN_GRACE_SECONDS)
    try:
        return max(0, int(raw))
    except ValueError:
        return int(DEFAULT_TOKEN_GRACE_SECONDS)


def max_review_timeout_seconds_for_revoke() -> int:
    """``OPR_MAX_REVIEW_TIMEOUT_SECONDS`` used when deriving the revoke retry window."""
    raw = os.environ.get("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return int(DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS)
    try:
        return max(1, int(raw))
    except ValueError:
        return int(DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS)


def derived_revoke_retry_window_seconds() -> float:
    """Default revoke evidence window: max review timeout + proxy token grace (ARB34-013)."""
    return float(max_review_timeout_seconds_for_revoke() + proxy_token_grace_seconds())


def revoke_retry_window_seconds() -> float:
    """Wall-clock window from job creation for revoke retries / deferred delete.

    Defaults to ``OPR_MAX_REVIEW_TIMEOUT_SECONDS + PROXY_TOKEN_GRACE_SECONDS`` so the
    launcher matches model-proxy job-token lifetime. An explicit
    ``LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS`` overrides when set (startup rejects
    values smaller than the derived sum — see ``ensure_revoke_retry_window_config``).
    """
    raw_window = os.environ.get("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "").strip()
    if raw_window:
        try:
            return max(1.0, float(raw_window))
        except ValueError:
            pass
    return derived_revoke_retry_window_seconds()


def ensure_revoke_retry_window_config() -> None:
    """Fail closed when an explicit revoke window is shorter than token lifetime."""
    raw_window = os.environ.get("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "").strip()
    if not raw_window:
        return
    try:
        explicit = float(raw_window)
    except ValueError as exc:
        raise RuntimeError(f"LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS ({raw_window!r}) must be a number") from exc
    derived = derived_revoke_retry_window_seconds()
    if explicit < derived:
        raise RuntimeError(
            f"LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS ({explicit}) must be >= "
            f"OPR_MAX_REVIEW_TIMEOUT_SECONDS ({max_review_timeout_seconds_for_revoke()}) + "
            f"PROXY_TOKEN_GRACE_SECONDS ({proxy_token_grace_seconds()}) = {derived}; "
            "raise the window or lower timeout/grace"
        )


def revoke_clock_skew_seconds() -> float:
    """``LAUNCHER_REVOKE_CLOCK_SKEW_SECONDS`` added to absolute token expiry (ARB34-014)."""
    raw = os.environ.get("LAUNCHER_REVOKE_CLOCK_SKEW_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_CLOCK_SKEW_SECONDS)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(DEFAULT_CLOCK_SKEW_SECONDS)


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
_CREATED_AT: dict[str, float] = {}  # diagnostic / weak fallback only
_EXPIRES_AT: dict[str, float] = {}  # absolute token expiry (latest wins, ARB34-014)
_ISSUED_AT: dict[str, float] = {}  # issuance-response receipt (latest wins)
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
    """Keep the **latest** creation/receipt anchor (conservative retention, ARB34-014)."""
    if created_at is None:
        return _CREATED_AT.get(job_id)
    cat = float(created_at)
    prev = _CREATED_AT.get(job_id)
    if prev is None or cat > prev:
        _CREATED_AT[job_id] = cat
        return cat
    return prev


def _remember_expires(job_id: str, expires_at: float | None) -> float | None:
    """Keep the **latest** absolute token expiry (most conservative deadline)."""
    if expires_at is None:
        return _EXPIRES_AT.get(job_id)
    exp = float(expires_at)
    prev = _EXPIRES_AT.get(job_id)
    if prev is None or exp > prev:
        _EXPIRES_AT[job_id] = exp
        return exp
    return prev


def _remember_issued(job_id: str, issued_at: float | None) -> float | None:
    """Keep the **latest** issuance-response receipt time for TTL fallback."""
    if issued_at is None:
        return _ISSUED_AT.get(job_id)
    iss = float(issued_at)
    prev = _ISSUED_AT.get(job_id)
    if prev is None or iss > prev:
        _ISSUED_AT[job_id] = iss
        return iss
    return prev


def effective_token_expires_at(
    job_id: str,
    *,
    expires_at: float | None = None,
    issued_at: float | None = None,
) -> float | None:
    """Absolute unix when the token is considered dead (before skew), or None if unknown."""
    jid = (job_id or "").strip()
    if not jid:
        return None
    with _PENDING_LOCK:
        exp = _remember_expires(jid, expires_at)
        if exp is not None:
            return float(exp)
        iss = _remember_issued(jid, issued_at)
        if iss is not None:
            return float(iss) + float(revoke_retry_window_seconds())
        return None


def revoke_delete_deadline_unix(
    job_id: str,
    *,
    expires_at: float | None = None,
    issued_at: float | None = None,
) -> float | None:
    """Unix time when evidence may be deleted for window expiry; None if unknown."""
    exp = effective_token_expires_at(job_id, expires_at=expires_at, issued_at=issued_at)
    if exp is None:
        return None
    return float(exp) + float(revoke_clock_skew_seconds())


def _prune_status_locked(now_mono: float) -> None:
    stale = [jid for jid, when in _CONFIRMED_AT.items() if (now_mono - when) >= DEFAULT_CONFIRMED_PRUNE_SECONDS]
    for jid in stale:
        _CONFIRMED_AT.pop(jid, None)
        if _STATUS.get(jid) is RevokeJobStatus.CONFIRMED:
            _STATUS.pop(jid, None)
            _CREATED_AT.pop(jid, None)
            _EXPIRES_AT.pop(jid, None)
            _ISSUED_AT.pop(jid, None)
    while len(_STATUS) > DEFAULT_STATUS_MAX:
        confirmed = [j for j, s in _STATUS.items() if s is RevokeJobStatus.CONFIRMED]
        if confirmed:
            victim = confirmed[0]
        elif _EXPIRES_AT:
            victim = min(_EXPIRES_AT, key=_EXPIRES_AT.get)  # type: ignore[arg-type]
        elif _CREATED_AT:
            victim = min(_CREATED_AT, key=_CREATED_AT.get)  # type: ignore[arg-type]
        else:
            victim = next(iter(_STATUS))
        _STATUS.pop(victim, None)
        _CONFIRMED_AT.pop(victim, None)
        _CREATED_AT.pop(victim, None)
        _EXPIRES_AT.pop(victim, None)
        _ISSUED_AT.pop(victim, None)


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
    expires_at: float | None = None,
    issued_at: float | None = None,
    now: float | None = None,
) -> RevokeJobStatus:
    """Non-blocking revoke status for ``job_id`` (reaper polls each pass)."""
    jid = (job_id or "").strip()
    if not jid:
        return RevokeJobStatus.CONFIRMED
    clock = time.time() if now is None else float(now)
    with _PENDING_LOCK:
        if created_at is not None:
            _remember_created(jid, created_at)
        if expires_at is not None:
            _remember_expires(jid, expires_at)
        if issued_at is not None:
            _remember_issued(jid, issued_at)
        st = _STATUS.get(jid)
        if st is RevokeJobStatus.CONFIRMED:
            return RevokeJobStatus.CONFIRMED
        # Deadline from expires_at (+skew) or issued_at+TTL; never from bare created_unix.
        exp = _EXPIRES_AT.get(jid)
        if exp is not None:
            deadline = float(exp) + float(revoke_clock_skew_seconds())
        else:
            iss = _ISSUED_AT.get(jid)
            if iss is not None:
                deadline = float(iss) + float(revoke_retry_window_seconds()) + float(revoke_clock_skew_seconds())
            else:
                deadline = None
        if deadline is not None and clock >= deadline:
            _STATUS[jid] = RevokeJobStatus.WINDOW_EXPIRED
            return RevokeJobStatus.WINDOW_EXPIRED
        if st is RevokeJobStatus.WINDOW_EXPIRED:
            return RevokeJobStatus.WINDOW_EXPIRED
        if st is RevokeJobStatus.CONFIG_ERROR:
            return RevokeJobStatus.CONFIG_ERROR
        if st is RevokeJobStatus.PENDING or jid in _ACTIVE_JOBS:
            return RevokeJobStatus.PENDING
        return RevokeJobStatus.PENDING


def known_token_expires(job_id: str, *, expires_at: float | None = None) -> bool:
    """True when an absolute/derived token expiry is known for ``job_id``."""
    jid = (job_id or "").strip()
    if not jid:
        return False
    with _PENDING_LOCK:
        if expires_at is not None:
            _remember_expires(jid, expires_at)
        return jid in _EXPIRES_AT or jid in _ISSUED_AT


def may_delete_after_revoke(
    job_id: str,
    *,
    created_at: float | None = None,
    expires_at: float | None = None,
    issued_at: float | None = None,
    now: float | None = None,
    orphan_ttl_elapsed: bool = False,
) -> bool:
    """True when reaper may remove container/checkout for ``job_id``.

    Confirmed revoke or window past token expiry (+skew). When no expiry is known
    (old jobs), ``orphan_ttl_elapsed`` is the retention backstop (ARB34-014).
    """
    st = revoke_status(
        job_id,
        created_at=created_at,
        expires_at=expires_at,
        issued_at=issued_at,
        now=now,
    )
    if st in (RevokeJobStatus.CONFIRMED, RevokeJobStatus.WINDOW_EXPIRED):
        return True
    if orphan_ttl_elapsed and not known_token_expires(job_id, expires_at=expires_at):
        return True
    return False


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
        expires = dict(_EXPIRES_AT)
        issued = dict(_ISSUED_AT)
    accepted = 0
    for jid, deadline in items:
        if deadline <= now:
            clear_revoke_needed(jid)
            continue
        if schedule_proxy_token_revoke(
            jid,
            created_at=created.get(jid),
            expires_at=expires.get(jid),
            issued_at=issued.get(jid),
        ):
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
    expires_at: float | None = None,
    issued_at: float | None = None,
) -> bool:
    """Best-effort background revoke; never blocks cancel/reaper.

    Returns True if accepted (queued, in-flight, already confirmed, or window
    already expired). Returns False if the pending queue is saturated.
    ``expires_at`` is the absolute proxy token expiry (preferred, ARB34-014).
    """
    global _PENDING
    jid = (job_id or "").strip()
    if not jid:
        return True
    budget = _cancel_revoke_timeout_seconds()
    max_pending = _revoke_max_pending()
    now = time.time()
    with _PENDING_LOCK:
        if created_at is not None:
            _remember_created(jid, created_at)
        if expires_at is not None:
            _remember_expires(jid, expires_at)
        if issued_at is not None:
            _remember_issued(jid, issued_at)
        if _STATUS.get(jid) is RevokeJobStatus.CONFIRMED:
            return True
        exp = _EXPIRES_AT.get(jid)
        iss = _ISSUED_AT.get(jid)
        has_known = exp is not None or iss is not None
        if exp is not None:
            deadline = float(exp) + float(revoke_clock_skew_seconds())
        elif iss is not None:
            deadline = float(iss) + float(revoke_retry_window_seconds()) + float(revoke_clock_skew_seconds())
        else:
            # No known expiry: do not mark WINDOW_EXPIRED; bound this attempt briefly
            # so the reaper can re-schedule (orphan TTL is the delete backstop).
            deadline = now + max(float(budget) * 3.0, 5.0)
        if has_known and now >= deadline:
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
        retry_deadline = float(deadline)
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
        _EXPIRES_AT.clear()
        _ISSUED_AT.clear()
        _CONFIRMED_AT.clear()
