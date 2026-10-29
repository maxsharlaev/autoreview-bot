"""Shared timeout budgets for container reviews (worker HTTP <-> launcher <-> Docker)."""

from __future__ import annotations

# Added to the job's timeout_seconds when waiting on the review container (jobs.py).
CONTAINER_WAIT_GRACE_SECONDS = 30
# Extra httpx timeout on Docker /containers/{id}/wait (docker_api.py).
DOCKER_WAIT_CLIENT_GRACE_SECONDS = 60
# Staging, create/start/logs/remove, and worker<->launcher network slack.
LAUNCHER_HTTP_MARGIN_SECONDS = 30


def launcher_pipeline_overhead_seconds() -> int:
    """Seconds beyond codex.timeout_seconds that the blocking launcher call may need."""
    return CONTAINER_WAIT_GRACE_SECONDS + DOCKER_WAIT_CLIENT_GRACE_SECONDS + LAUNCHER_HTTP_MARGIN_SECONDS


def launcher_http_timeout_seconds(timeout_seconds: int) -> float:
    """Read timeout for the worker's POST /v1/reviews call."""
    return float(int(timeout_seconds) + launcher_pipeline_overhead_seconds())


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
