"""Shared timeout budgets for container reviews (worker HTTP <-> launcher <-> Docker)."""

from __future__ import annotations

import os

# Added to the job's timeout_seconds when waiting on the review container (jobs.py).
CONTAINER_WAIT_GRACE_SECONDS = 30
# Extra httpx timeout on Docker /containers/{id}/wait (docker_api.py).
DOCKER_WAIT_CLIENT_GRACE_SECONDS = 60
# Staging, create/start/logs/remove, and worker<->launcher network slack.
LAUNCHER_HTTP_MARGIN_SECONDS = 30
# Cancel coordination: whole cancel (list/kill/remove + wait) must stay below worker DELETE read.
DEFAULT_LAUNCHER_CANCEL_DEADLINE_SECONDS = 4.0
# Lifecycle idle wait inside that overall deadline (kept for kill-first coordination).
DEFAULT_LAUNCHER_CANCEL_WAIT_SECONDS = 3.0
# Back-compat aliases (defaults); prefer launcher_cancel_*_seconds() at runtime.
LAUNCHER_CANCEL_DEADLINE_SECONDS = DEFAULT_LAUNCHER_CANCEL_DEADLINE_SECONDS
LAUNCHER_CANCEL_WAIT_SECONDS = DEFAULT_LAUNCHER_CANCEL_WAIT_SECONDS
WORKER_CANCEL_HTTP_READ_SECONDS = 5.0
WORKER_CANCEL_HTTP_CONNECT_SECONDS = 2.0
# Container orphan reaper: max review + wait grace (not clone+review coupling).
REVIEW_CONTAINER_ORPHAN_GRACE_SECONDS = CONTAINER_WAIT_GRACE_SECONDS + DOCKER_WAIT_CLIENT_GRACE_SECONDS
# Clone may finish before review starts; orphan TTL must cover clone + review.
# Also the hard cap enforced by git_clone.clone_head (OPR_CLONE_TIMEOUT_SECONDS).
DEFAULT_CLONE_BUDGET_SECONDS = 300

# Matches CodexYaml.timeout_seconds / job.json default when unset.
DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS = 600


def launcher_pipeline_overhead_seconds() -> int:
    """Seconds beyond codex.timeout_seconds that the blocking launcher call may need."""
    return CONTAINER_WAIT_GRACE_SECONDS + DOCKER_WAIT_CLIENT_GRACE_SECONDS + LAUNCHER_HTTP_MARGIN_SECONDS


def clone_budget_seconds() -> int:
    """Max expected clone duration covered by orphan TTL (``OPR_CLONE_TIMEOUT_SECONDS``)."""
    raw = os.environ.get("OPR_CLONE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CLONE_BUDGET_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_CLONE_BUDGET_SECONDS


def launcher_cancel_deadline_seconds() -> float:
    """Overall seconds for DELETE cancel incl. Docker calls (``LAUNCHER_CANCEL_DEADLINE_SECONDS``)."""
    raw = os.environ.get("LAUNCHER_CANCEL_DEADLINE_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_LAUNCHER_CANCEL_DEADLINE_SECONDS)
    try:
        return max(1.0, float(raw))
    except ValueError:
        return float(DEFAULT_LAUNCHER_CANCEL_DEADLINE_SECONDS)


def launcher_cancel_wait_seconds() -> float:
    """Seconds launcher waits after kill-first cancel (``LAUNCHER_CANCEL_WAIT_SECONDS``)."""
    raw = os.environ.get("LAUNCHER_CANCEL_WAIT_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_LAUNCHER_CANCEL_WAIT_SECONDS)
    try:
        return max(0.5, float(raw))
    except ValueError:
        return float(DEFAULT_LAUNCHER_CANCEL_WAIT_SECONDS)


def launcher_http_timeout_seconds(timeout_seconds: int) -> float:
    """Read timeout for the worker's POST /v1/reviews call."""
    return float(int(timeout_seconds) + launcher_pipeline_overhead_seconds())


def max_review_timeout_seconds() -> int:
    """Max review timeout the launcher accepts (``OPR_MAX_REVIEW_TIMEOUT_SECONDS``)."""
    raw = os.environ.get("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS


def min_review_container_orphan_ttl_seconds() -> int:
    """Shortest safe TTL for labeled review containers (max review + wait grace)."""
    return int(max_review_timeout_seconds()) + int(REVIEW_CONTAINER_ORPHAN_GRACE_SECONDS)


def review_orphan_ttl_seconds() -> int:
    """``OPR_REVIEW_ORPHAN_TTL_SECONDS`` (container reaper), default 7200."""
    raw = os.environ.get("OPR_REVIEW_ORPHAN_TTL_SECONDS", "7200").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 7200


# Back-compat aliases used by older call sites / tests (no-lease checkout age bound).
def min_checkout_orphan_ttl_seconds(timeout_seconds: int) -> int:
    """Deprecated coupling bound; kept for tests. Prefer lease + no_lease_max_age."""
    return clone_budget_seconds() + int(timeout_seconds) + launcher_pipeline_overhead_seconds()


def default_checkout_orphan_ttl_seconds() -> int:
    """No-lease checkout age default when ``OPR_CHECKOUT_ORPHAN_TTL_SECONDS`` unset."""
    from app.launcher.lease import no_lease_max_age_seconds

    return int(no_lease_max_age_seconds())


def checkout_orphan_ttl_seconds() -> int:
    """No-lease checkout age bound (``OPR_CHECKOUT_ORPHAN_TTL_SECONDS`` or default)."""
    from app.launcher.lease import no_lease_max_age_seconds

    return int(no_lease_max_age_seconds())


def validate_codex_timeout_against_arq(*, timeout_seconds: int, worker_job_timeout: int) -> None:
    """Fail closed when a container review can outlive the ARQ job timeout."""
    overhead = launcher_pipeline_overhead_seconds()
    needed = int(timeout_seconds) + overhead
    limit = int(worker_job_timeout)
    if needed >= limit:
        raise RuntimeError(
            f"codex.timeout_seconds ({timeout_seconds}) + launcher overhead ({overhead}s) "
            f"= {needed}s must be < worker_job_timeout / WORKER_JOB_TIMEOUT ({limit}s); "
            "raise WORKER_JOB_TIMEOUT or lower codex.timeout_seconds"
        )


def validate_codex_timeout_against_max_review(*, timeout_seconds: int) -> None:
    """Fail closed when codex.timeout_seconds exceeds OPR_MAX_REVIEW_TIMEOUT_SECONDS."""
    limit = int(max_review_timeout_seconds())
    if int(timeout_seconds) > limit:
        raise RuntimeError(
            f"codex.timeout_seconds ({timeout_seconds}) must be <= "
            f"OPR_MAX_REVIEW_TIMEOUT_SECONDS ({limit}); "
            "raise OPR_MAX_REVIEW_TIMEOUT_SECONDS or lower codex.timeout_seconds"
        )


def validate_checkout_orphan_ttl(*, timeout_seconds: int, orphan_ttl_seconds: int | None = None) -> None:
    """Legacy no-op for call sites; checkout dirs are lease-protected.

    Kept so older workers/tests that still call this do not break. Live checkouts
    are covered by ``.lease`` heartbeats; ``OPR_CHECKOUT_ORPHAN_TTL_SECONDS`` is
    only the no-lease crash fallback (see ``no_lease_max_age_seconds``).
    """
    _ = (timeout_seconds, orphan_ttl_seconds)


def validate_review_container_orphan_ttl(*, orphan_ttl_seconds: int | None = None) -> None:
    """Fail closed when container orphan TTL can expire during a max-length review."""
    ttl = review_orphan_ttl_seconds() if orphan_ttl_seconds is None else int(orphan_ttl_seconds)
    needed = min_review_container_orphan_ttl_seconds()
    if ttl < needed:
        raise RuntimeError(
            f"OPR_REVIEW_ORPHAN_TTL_SECONDS ({ttl}s) must be >= max review timeout "
            f"({max_review_timeout_seconds()}s) + container wait grace "
            f"({REVIEW_CONTAINER_ORPHAN_GRACE_SECONDS}s) = {needed}s; "
            "raise OPR_REVIEW_ORPHAN_TTL_SECONDS or lower OPR_MAX_REVIEW_TIMEOUT_SECONDS"
        )


def validate_cancel_timeouts() -> None:
    """Fail closed when cancel deadline/wait can exceed worker DELETE read timeout."""
    wait = launcher_cancel_wait_seconds()
    deadline = launcher_cancel_deadline_seconds()
    if not (wait < deadline):
        raise RuntimeError(
            f"LAUNCHER_CANCEL_WAIT_SECONDS ({wait}) must be < LAUNCHER_CANCEL_DEADLINE_SECONDS ({deadline})"
        )
    if not (deadline < WORKER_CANCEL_HTTP_READ_SECONDS):
        raise RuntimeError(
            f"LAUNCHER_CANCEL_DEADLINE_SECONDS ({deadline}) must be < "
            f"WORKER_CANCEL_HTTP_READ_SECONDS ({WORKER_CANCEL_HTTP_READ_SECONDS})"
        )


def ensure_launcher_orphan_ttl_config() -> None:
    """Launcher startup: container orphan TTL + cancel timeout ordering."""
    validate_review_container_orphan_ttl()
    validate_cancel_timeouts()
