"""Shared timeout budgets for container reviews (worker HTTP <-> launcher <-> Docker)."""

from __future__ import annotations

import os

# Added to the job's timeout_seconds when waiting on the review container (jobs.py).
CONTAINER_WAIT_GRACE_SECONDS = 30
# Extra httpx timeout on Docker /containers/{id}/wait (docker_api.py).
DOCKER_WAIT_CLIENT_GRACE_SECONDS = 60
# Staging, create/start/logs/remove, and worker<->launcher network slack.
LAUNCHER_HTTP_MARGIN_SECONDS = 30
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


def min_checkout_orphan_ttl_seconds(timeout_seconds: int) -> int:
    """Shortest safe orphan TTL for clone + a review of ``timeout_seconds``.

    Clone dirs are created before the review window and kept until cleanup after
    the review; mtime-based reaping must not remove them while either phase runs.
    """
    return clone_budget_seconds() + int(timeout_seconds) + launcher_pipeline_overhead_seconds()


def default_checkout_orphan_ttl_seconds() -> int:
    """Derived default: clone budget + max review timeout + launcher pipeline overhead."""
    return min_checkout_orphan_ttl_seconds(max_review_timeout_seconds())


def checkout_orphan_ttl_seconds() -> int:
    """Configured orphan TTL, or the derived default when unset/invalid."""
    raw = os.environ.get("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "").strip()
    if not raw:
        return default_checkout_orphan_ttl_seconds()
    try:
        return max(60, int(raw))
    except ValueError:
        return default_checkout_orphan_ttl_seconds()


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


def validate_checkout_orphan_ttl(*, timeout_seconds: int, orphan_ttl_seconds: int | None = None) -> None:
    """Fail closed when orphan TTL can expire during clone or a live review."""
    ttl = checkout_orphan_ttl_seconds() if orphan_ttl_seconds is None else int(orphan_ttl_seconds)
    needed = min_checkout_orphan_ttl_seconds(timeout_seconds)
    clone_b = clone_budget_seconds()
    overhead = launcher_pipeline_overhead_seconds()
    if ttl < needed:
        raise RuntimeError(
            f"OPR_CHECKOUT_ORPHAN_TTL_SECONDS ({ttl}s) must be >= clone budget "
            f"({clone_b}s) + review timeout ({timeout_seconds}s) + launcher overhead "
            f"({overhead}s) = {needed}s; raise OPR_CHECKOUT_ORPHAN_TTL_SECONDS "
            "(or OPR_MAX_REVIEW_TIMEOUT_SECONDS / OPR_CLONE_TIMEOUT_SECONDS) "
            "or lower the review timeout"
        )


def ensure_launcher_orphan_ttl_config() -> None:
    """Launcher startup: orphan TTL must cover clone budget + max review timeout."""
    validate_checkout_orphan_ttl(timeout_seconds=max_review_timeout_seconds())
