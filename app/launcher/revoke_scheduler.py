"""Bounded proxy token revoke scheduler with retry until token TTL (ARB34-005/009).

Revocations run on a dedicated long-lived asyncio loop with ``asyncio.timeout``
around the async HTTP client (same wall-clock pattern as AR-030 / #30). Pending
work is bounded; ``schedule_proxy_token_revoke`` returns whether the job was
accepted. Same ``job_id`` is deduped (one in-flight/queued revoke). Transient
failures (timeout, connection, 5xx) retry with exponential backoff until success
or the max token TTL window elapses (``OPR_MAX_REVIEW_TIMEOUT_SECONDS`` +
``PROXY_TOKEN_GRACE_SECONDS`` from first attempt; issue time is not known here).

There are **no persistent tombstones**: a launcher restart loses the in-memory
retry set and in-flight work; worst case a token remains valid until its TTL.
Callers that must not lose revoke evidence (reaper) must keep the
container/checkout when schedule is rejected and retry next pass.
``_CANCEL_POOL`` stays exclusive to Docker cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CANCEL_REVOKE_TIMEOUT_SECONDS = 2.0
DEFAULT_REVOKE_MAX_PENDING = 32
DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS = 600
DEFAULT_TOKEN_GRACE_SECONDS = 60
DEFAULT_RETRY_INITIAL_BACKOFF_SECONDS = 0.5
DEFAULT_RETRY_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_RETRY_SET_MAX = 256


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


def _max_token_ttl_seconds() -> float:
    """Retry window upper bound (matches model-proxy max token TTL by default).

    Explicit ``LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS`` wins. Otherwise
    ``OPR_MAX_REVIEW_TIMEOUT_SECONDS`` + ``PROXY_TOKEN_GRACE_SECONDS`` (defaults
    600 + 60) — grace may come from the process env without being listed in the
    launcher Compose ``environment`` (TOKEN-named keys are forbidden there).
    """
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
        """Fire-and-forget on the revoke loop (errors logged inside the coro)."""
        loop = self.ensure_started()
        asyncio.run_coroutine_threadsafe(coro, loop)


_REVOKE_LOOP = _RevokeAsyncLoop()
_PENDING = 0
_PENDING_LOCK = threading.Lock()
# job_ids with an accepted in-flight/queued revoke coroutine (dedup).
_ACTIVE_JOBS: set[str] = set()
# job_id -> unix deadline: keep trying to schedule until then (size+TTL bounded).
_RETRY_NEEDED: dict[str, float] = {}


def _dec_pending() -> None:
    global _PENDING
    with _PENDING_LOCK:
        _PENDING = max(0, _PENDING - 1)


def pending_revoke_count() -> int:
    """Test helper: in-flight + queued revoke coroutines that hold a slot."""
    with _PENDING_LOCK:
        return int(_PENDING)


def active_revoke_job_ids() -> set[str]:
    """Test helper: job ids currently covered by an accepted revoke worker."""
    with _PENDING_LOCK:
        return set(_ACTIVE_JOBS)


def retry_needed_job_ids() -> set[str]:
    """Test helper: job ids waiting for a later schedule attempt."""
    with _PENDING_LOCK:
        return set(_RETRY_NEEDED)


def _clear_active(job_id: str) -> None:
    with _PENDING_LOCK:
        _ACTIVE_JOBS.discard(job_id)


def note_revoke_needed(job_id: str, *, deadline: float | None = None) -> None:
    """Record that ``job_id`` still needs a revoke (queue full or cancel reject).

    Bounded by ``LAUNCHER_REVOKE_RETRY_SET_MAX`` and per-entry TTL (max token TTL).
    No disk persistence — lost on launcher restart (token TTL is the outer bound).
    """
    jid = (job_id or "").strip()
    if not jid:
        return
    now = time.time()
    until = float(deadline) if deadline is not None else now + _max_token_ttl_seconds()
    with _PENDING_LOCK:
        # Drop expired entries first.
        expired = [k for k, d in _RETRY_NEEDED.items() if d <= now]
        for k in expired:
            _RETRY_NEEDED.pop(k, None)
        if jid in _RETRY_NEEDED:
            _RETRY_NEEDED[jid] = max(_RETRY_NEEDED[jid], until)
            return
        if len(_RETRY_NEEDED) >= _retry_set_max():
            # Evict soonest-expiring entry to keep the set bounded.
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
    """Try to schedule every still-needed revoke. Returns newly accepted count.

    Safe to call from the reaper (non-blocking). Expired entries are dropped.
    """
    now = time.time()
    with _PENDING_LOCK:
        items = list(_RETRY_NEEDED.items())
    accepted = 0
    for jid, deadline in items:
        if deadline <= now:
            clear_revoke_needed(jid)
            continue
        if schedule_proxy_token_revoke(jid):
            clear_revoke_needed(jid)
            accepted += 1
    return accepted


async def _revoke_with_retries(job_id: str, budget: float, retry_deadline: float) -> None:
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
                return

            now = time.time()
            if now >= retry_deadline:
                logger.warning(
                    "background proxy revoke giving up job=%s after token TTL window",
                    job_id,
                )
                # Past TTL the token is dead anyway; drop retry marker.
                clear_revoke_needed(job_id)
                return

            sleep_for = min(backoff, max(0.0, retry_deadline - now))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            backoff = min(max_backoff, backoff * 2.0)
    finally:
        _clear_active(job_id)
        _dec_pending()


def schedule_proxy_token_revoke(job_id: str) -> bool:
    """Best-effort background revoke; never blocks the cancel/reaper path.

    Returns True if accepted (queued, or already in-flight for this job_id).
    Returns False if the pending queue is saturated — caller (reaper) must keep
    evidence and retry later; cancel should ``note_revoke_needed``.
    """
    global _PENDING
    jid = (job_id or "").strip()
    if not jid:
        return True
    budget = _cancel_revoke_timeout_seconds()
    max_pending = _revoke_max_pending()
    retry_deadline = time.time() + _max_token_ttl_seconds()
    with _PENDING_LOCK:
        if jid in _ACTIVE_JOBS:
            # Already covered — treat as accepted (no double concurrent revoke).
            return True
        if _PENDING >= max_pending:
            logger.warning(
                "proxy revoke queue saturated pending=%s max=%s; rejecting job=%s "
                "(caller must retain evidence / retry; no persistent tombstone — "
                "launcher restart worst case is token TTL)",
                _PENDING,
                max_pending,
                jid,
            )
            return False
        _PENDING += 1
        _ACTIVE_JOBS.add(jid)
    try:
        _REVOKE_LOOP.submit(_revoke_with_retries(jid, budget, retry_deadline))
    except Exception:  # noqa: BLE001
        _clear_active(jid)
        _dec_pending()
        logger.warning("background proxy revoke submit failed job=%s", jid, exc_info=True)
        return False
    return True


def _reset_for_tests() -> None:
    """Test helper: clear counters / sets (does not stop the async loop)."""
    global _PENDING
    with _PENDING_LOCK:
        _PENDING = 0
        _ACTIVE_JOBS.clear()
        _RETRY_NEEDED.clear()
