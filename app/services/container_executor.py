"""Container ReviewExecutor: stage job files and call the launcher (M2)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from pathlib import Path

import httpx

from app.services.codex_runner import CodexRunnerError
from app.services.constants import CODEX_SANDBOX

logger = logging.getLogger(__name__)


class ContainerReviewExecutor:
    """Talks to the dedicated launcher; never holds the Docker socket itself.

    Any launcher/container failure becomes ``CODEX_SANDBOX`` (fail closed).
    There is no fallback to ``in_process``.
    """

    def __init__(
        self,
        *,
        launcher_url: str | None = None,
        launcher_token: str | None = None,
        checkout_base: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._launcher_url = (launcher_url or os.environ.get("LAUNCHER_URL") or "http://launcher:8080").rstrip("/")
        self._launcher_token = (
            launcher_token if launcher_token is not None else os.environ.get("LAUNCHER_TOKEN", "")
        ).strip()
        self._checkout_base = Path(checkout_base or os.environ.get("OPR_CHECKOUT_BASE") or "/var/opr/checkouts")
        self._http_client = http_client

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
        reasoning_effort: str = "medium",
        sandbox: str = "read-only",
        approval_policy: str = "never",
        allow_unsandboxed: bool = False,
    ) -> str:
        if not self._launcher_token or len(self._launcher_token) < 16:
            raise CodexRunnerError(CODEX_SANDBOX, "LAUNCHER_TOKEN missing or too short (fail closed)")
        if not (openai_api_key or "").strip():
            raise CodexRunnerError(CODEX_SANDBOX, "model api key empty for container backend (fail closed)")

        job_id = uuid.uuid4().hex
        base = self._checkout_base
        base.mkdir(parents=True, exist_ok=True)
        job_dir = base / job_id
        repo_dir = job_dir / "repo"
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            self._stage_checkout(checkout, repo_dir)
            (job_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            shutil.copyfile(schema_path, job_dir / "schema.json")
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "model": model,
                        "timeout_seconds": timeout_seconds,
                        "reasoning_effort": reasoning_effort,
                        "sandbox": sandbox,
                        "approval_policy": approval_policy,
                        "allow_unsandboxed": allow_unsandboxed,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            checkout_ref = f"{job_id}/repo"
            text = await self._call_launcher(job_id=job_id, checkout_ref=checkout_ref, model_api_key=openai_api_key)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(text, encoding="utf-8")
            return text
        except CodexRunnerError:
            raise
        except Exception as exc:  # noqa: BLE001 - map unexpected staging/HTTP errors to fail-closed
            raise CodexRunnerError(CODEX_SANDBOX, f"container executor failed: {exc}") from exc
        finally:
            # Keep checkout for orchestrator cleanup; remove only our staging copy.
            if job_dir.exists() and repo_dir.resolve() != checkout.resolve():
                shutil.rmtree(job_dir, ignore_errors=True)

    def _stage_checkout(self, checkout: Path, repo_dir: Path) -> None:
        src = checkout.resolve()
        # If the worker already cloned under the shared base as .../job/repo, reuse.
        try:
            src.relative_to(self._checkout_base.resolve())
            if src.name == "repo" and src.parent.parent == self._checkout_base.resolve():
                # Already a staged job layout; copy tree still isolates launcher path.
                pass
        except ValueError:
            pass
        shutil.copytree(src, repo_dir, dirs_exist_ok=False)

    async def _call_launcher(self, *, job_id: str, checkout_ref: str, model_api_key: str) -> str:
        headers = {
            "Authorization": f"Bearer {self._launcher_token}",
            "X-Model-Api-Key": model_api_key,
            "Content-Type": "application/json",
        }
        payload = {"job_id": job_id, "checkout_ref": checkout_ref}
        owns = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0))
        try:
            response = await client.post(f"{self._launcher_url}/v1/reviews", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise CodexRunnerError(CODEX_SANDBOX, f"launcher unreachable: {exc}") from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code == 401:
            raise CodexRunnerError(CODEX_SANDBOX, "launcher auth failed (fail closed)")
        if response.status_code >= 400:
            detail = _safe_detail(response)
            code = detail.get("code") if isinstance(detail, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else None
            raise CodexRunnerError(
                code if code == CODEX_SANDBOX else CODEX_SANDBOX,
                message or f"launcher error HTTP {response.status_code}",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise CodexRunnerError(CODEX_SANDBOX, "launcher returned non-JSON") from exc
        output = data.get("output")
        if not isinstance(output, str) or not output.strip():
            raise CodexRunnerError(CODEX_SANDBOX, "launcher returned empty output")
        return output


def _safe_detail(response: httpx.Response) -> object:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict) and "detail" in data:
        return data["detail"]
    return data
