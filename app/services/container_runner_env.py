"""Validate container-runner env on the worker when codex.runner_backend is container."""

from __future__ import annotations

import os

from app.launcher.timeouts import (
    validate_checkout_orphan_ttl,
    validate_codex_timeout_against_arq,
)


def ensure_container_runner_env(
    *,
    runner_backend: str,
    timeout_seconds: int | None = None,
    worker_job_timeout: int | None = None,
) -> None:
    """Raise RuntimeError when container backend is selected without a launcher token.

    Compose keeps `LAUNCHER_TOKEN` optional for interpolation so the default
    `in_process` stack and CI work without the variable; selecting `container`
    must fail closed on the worker before any review runs (the API does not call the launcher).

    Also requires `codex.timeout_seconds` plus launcher overhead to stay below
    the ARQ `worker_job_timeout` so the worker is not cancelled mid-launcher call,
    and `OPR_CHECKOUT_ORPHAN_TTL_SECONDS` to cover that same review lifetime.
    """
    if (runner_backend or "").strip() != "container":
        return
    token = os.environ.get("LAUNCHER_TOKEN", "").strip()
    if len(token) < 16:
        raise RuntimeError(
            "codex.runner_backend=container requires LAUNCHER_TOKEN "
            "(>= 16 chars) shared with the launcher service; refusing to start"
        )
    if timeout_seconds is not None and worker_job_timeout is not None:
        validate_codex_timeout_against_arq(
            timeout_seconds=timeout_seconds,
            worker_job_timeout=worker_job_timeout,
        )
    if timeout_seconds is not None:
        validate_checkout_orphan_ttl(timeout_seconds=timeout_seconds)
