"""Container ReviewExecutor: stage job files and call the launcher (M2)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

import httpx

from app.launcher.timeouts import launcher_http_timeout_seconds
from app.services.checkout_safety import safe_copytree
from app.services.codex_runner import CodexRunnerError
from app.services.constants import CODEX_SANDBOX, CODEX_TIMEOUT

logger = logging.getLogger(__name__)

# Short timeout for cancel DELETE so it can run under asyncio.shield.
_CANCEL_HTTP_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)


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
        from app.launcher.token import launcher_token_is_configured

        if not launcher_token_is_configured(self._launcher_token):
            raise CodexRunnerError(CODEX_SANDBOX, "LAUNCHER_TOKEN missing or too short (fail closed)")
        if not (openai_api_key or "").strip():
            raise CodexRunnerError(CODEX_SANDBOX, "model api key empty for container backend (fail closed)")

        job_id = uuid.uuid4().hex
        base = self._checkout_base
        base.mkdir(parents=True, exist_ok=True)
        job_dir = base / job_id
        repo_dir = job_dir / "repo"
        remove_job_dir = True
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
            try:
                text = await self._call_launcher(
                    job_id=job_id,
                    checkout_ref=checkout_ref,
                    model_api_key=openai_api_key,
                    timeout_seconds=timeout_seconds,
                )
            except asyncio.CancelledError:
                # ARQ/job cancellation: ask launcher to kill the container, then drop
                # staging only if cancel was acknowledged (else leave for reaper).
                remove_job_dir = False
                if await self._cancel_job_shielded(job_id):
                    remove_job_dir = True
                raise
            except CodexRunnerError as exc:
                if exc.code == CODEX_TIMEOUT:
                    remove_job_dir = False
                    if await self._cancel_job_shielded(job_id):
                        remove_job_dir = True
                raise
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(text, encoding="utf-8")
            return text
        except CodexRunnerError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - map unexpected staging/HTTP errors to fail-closed
            raise CodexRunnerError(CODEX_SANDBOX, f"container executor failed: {exc}") from exc
        finally:
            # Keep checkout for orchestrator cleanup; remove only our staging copy when safe.
            if remove_job_dir and job_dir.exists() and repo_dir.resolve() != checkout.resolve():
                shutil.rmtree(job_dir, ignore_errors=True)

    def _stage_checkout(self, checkout: Path, repo_dir: Path) -> None:
        src = checkout.resolve()
        try:
            src.relative_to(self._checkout_base.resolve())
            if src.name == "repo" and src.parent.parent == self._checkout_base.resolve():
                pass
        except ValueError:
            pass
        safe_copytree(src, repo_dir)

    async def _cancel_job_shielded(self, job_id: str) -> bool:
        """Best-effort cancel that survives surrounding task cancellation."""
        try:
            return bool(await asyncio.shield(self._cancel_job(job_id)))
        except Exception:  # noqa: BLE001 - never let cancel errors mask CancelledError
            logger.warning("launcher cancel failed for job_id=%s", job_id, exc_info=True)
            return False

    async def _cancel_job(self, job_id: str) -> bool:
        """DELETE /v1/jobs/{id}. True when launcher confirmed (including already-gone)."""
        headers = {"Authorization": f"Bearer {self._launcher_token}"}
        owns = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=_CANCEL_HTTP_TIMEOUT)
        try:
            response = await client.delete(f"{self._launcher_url}/v1/jobs/{job_id}", headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("launcher cancel unreachable job_id=%s: %s", job_id, exc)
            return False
        finally:
            if owns:
                await client.aclose()
        if response.status_code == 404:
            # Unknown to HTTP layer but UUID path should not 404; treat as unconfirmed.
            return False
        if response.status_code not in (200, 204):
            logger.warning(
                "launcher cancel HTTP %s for job_id=%s body=%s",
                response.status_code,
                job_id,
                response.text[:200],
            )
            return False
        try:
            data = response.json()
        except ValueError:
            return False
        # Staging may be removed only when the launcher confirms nothing remains.
        return bool(isinstance(data, dict) and data.get("confirmed") is True)

    async def _call_launcher(self, *, job_id: str, checkout_ref: str, model_api_key: str, timeout_seconds: int) -> str:
        headers = {
            "Authorization": f"Bearer {self._launcher_token}",
            "X-Model-Api-Key": model_api_key,
            "Content-Type": "application/json",
        }
        payload = {"job_id": job_id, "checkout_ref": checkout_ref}
        owns = self._http_client is None
        read_timeout = launcher_http_timeout_seconds(timeout_seconds)
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=60.0, pool=10.0)
        )
        try:
            response = await client.post(f"{self._launcher_url}/v1/reviews", headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise CodexRunnerError(CODEX_TIMEOUT, f"launcher call timed out: {exc}") from exc
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
