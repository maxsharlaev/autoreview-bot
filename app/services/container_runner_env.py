"""Validate container-runner env when codex.runner_backend is container."""

from __future__ import annotations

import os


def ensure_container_runner_env(*, runner_backend: str) -> None:
    """Raise RuntimeError when container backend is selected without a launcher token.

    Compose keeps ``LAUNCHER_TOKEN`` optional for interpolation so the default
    ``in_process`` stack and CI work without the variable; selecting ``container``
    must fail closed before any review runs.
    """
    if (runner_backend or "").strip() != "container":
        return
    token = os.environ.get("LAUNCHER_TOKEN", "").strip()
    if len(token) < 16:
        raise RuntimeError(
            "codex.runner_backend=container requires LAUNCHER_TOKEN "
            "(>= 16 chars) shared with the launcher service; refusing to start"
        )
