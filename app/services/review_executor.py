"""Review execution backends: in-process Codex or ephemeral container via launcher."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.services.checkout_safety import neutralize_escaping_symlinks
from app.services.codex_runner import run_codex

IN_PROCESS_BACKEND = "in_process"
CONTAINER_BACKEND = "container"
SUPPORTED_RUNNER_BACKENDS = frozenset({IN_PROCESS_BACKEND, CONTAINER_BACKEND})


@runtime_checkable
class ReviewExecutor(Protocol):
    """Run Codex against a prepared checkout and return the raw model output text."""

    async def run(
        self,
        *,
        checkout: Path,
        prompt: str,
        schema_path: Path,
        output_path: Path,
        model: str,
        timeout_seconds: int,
        openai_api_key: str,
        owner_id: str = "",
        reasoning_effort: str = "medium",
        sandbox: str = "read-only",
        approval_policy: str = "never",
        allow_unsandboxed: bool = False,
    ) -> str: ...


class InProcessReviewExecutor:
    """Current worker path: ``run_codex`` in the worker process (zero behavior change)."""

    async def run(
        self,
        *,
        checkout: Path,
        prompt: str,
        schema_path: Path,
        output_path: Path,
        model: str,
        timeout_seconds: int,
        openai_api_key: str,
        owner_id: str = "",
        reasoning_effort: str = "medium",
        sandbox: str = "read-only",
        approval_policy: str = "never",
        allow_unsandboxed: bool = False,
    ) -> str:
        _ = owner_id  # container-only; in_process uses openai_api_key directly
        # Defense in depth: drop escaping symlinks before in-process Codex sees the tree.
        # Off the event loop so lease heartbeats / other async work are not starved.
        await asyncio.to_thread(neutralize_escaping_symlinks, checkout)
        return await run_codex(
            checkout=checkout,
            prompt=prompt,
            schema_path=schema_path,
            output_path=output_path,
            model=model,
            timeout_seconds=timeout_seconds,
            openai_api_key=openai_api_key,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            approval_policy=approval_policy,
            allow_unsandboxed=allow_unsandboxed,
        )


def get_review_executor(backend: str) -> ReviewExecutor:
    """Return the executor for ``codex.runner_backend``."""
    name = (backend or "").strip()
    if name == IN_PROCESS_BACKEND:
        return InProcessReviewExecutor()
    if name == CONTAINER_BACKEND:
        from app.services.container_executor import ContainerReviewExecutor

        return ContainerReviewExecutor()
    raise ValueError(f"unknown codex.runner_backend={name!r}; supported: {IN_PROCESS_BACKEND!r}, {CONTAINER_BACKEND!r}")
