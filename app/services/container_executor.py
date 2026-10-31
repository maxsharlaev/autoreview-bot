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

from app.launcher.lease import (
    remove_directory_lease_file,
    start_directory_lease,
    stop_directory_lease,
)
from app.launcher.timeouts import (
    WORKER_CANCEL_HTTP_CONNECT_SECONDS,
    WORKER_CANCEL_HTTP_READ_SECONDS,
    launcher_http_timeout_seconds,
)
from app.services.checkout_safety import safe_copytree
from app.services.codex_runner import CodexRunnerError
from app.services.constants import CODEX_SANDBOX, CODEX_TIMEOUT, CODEX_UPGRADE_REQUIRED
from app.services.model_proxy_client import (
    ModelProxyIssuerError,
    issue_job_token,
    revoke_job_token,
)

logger = logging.getLogger(__name__)

# Finish-path proxy revoke budget (ARB34-004); reaper/launcher remain backstops.
DEFAULT_FINISH_REVOKE_TIMEOUT_SECONDS = 2.0


def finish_revoke_timeout_seconds() -> float:
    raw = os.environ.get("OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_FINISH_REVOKE_TIMEOUT_SECONDS)
    try:
        return max(0.1, float(raw))
    except ValueError:
        return float(DEFAULT_FINISH_REVOKE_TIMEOUT_SECONDS)


# Short timeout for cancel DELETE so it can run under asyncio.shield.
_CANCEL_HTTP_TIMEOUT = httpx.Timeout(
    connect=WORKER_CANCEL_HTTP_CONNECT_SECONDS,
    read=WORKER_CANCEL_HTTP_READ_SECONDS,
    write=WORKER_CANCEL_HTTP_READ_SECONDS,
    pool=WORKER_CANCEL_HTTP_CONNECT_SECONDS,
)


def _remove_job_dir(job_dir: Path) -> None:
    """Remove staged job dir and its sidecar lease."""
    shutil.rmtree(job_dir, ignore_errors=True)
    remove_directory_lease_file(job_dir)


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
        owner_id: str = "",
        reasoning_effort: str = "medium",
        sandbox: str = "read-only",
        approval_policy: str = "never",
        allow_unsandboxed: bool = False,
    ) -> str:
        from app.launcher.token import launcher_token_is_configured

        if not launcher_token_is_configured(self._launcher_token):
            raise CodexRunnerError(CODEX_SANDBOX, "LAUNCHER_TOKEN missing or too short (fail closed)")
        # Real model keys stay on model-proxy; container path must not require/forward them.
        _ = openai_api_key  # protocol compat; unused for container (M3)

        job_id = uuid.uuid4().hex
        base = self._checkout_base
        base.mkdir(parents=True, exist_ok=True)
        job_dir = base / job_id
        repo_dir = job_dir / "repo"
        remove_job_dir = True
        proxy_token: str | None = None
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            await start_directory_lease(job_dir)
            # Blocking FS work off the event loop so the lease heartbeat can refresh.
            await asyncio.to_thread(
                self._stage_job_files,
                checkout,
                repo_dir,
                job_dir,
                prompt,
                schema_path,
                model,
                timeout_seconds,
                reasoning_effort,
                sandbox,
                approval_policy,
                allow_unsandboxed,
            )
            try:
                proxy_token = await issue_job_token(
                    job_id=job_id,
                    timeout_seconds=timeout_seconds,
                    owner_id=owner_id or "",
                    client=self._http_client,
                )
            except ModelProxyIssuerError as exc:
                raise CodexRunnerError(CODEX_SANDBOX, f"model-proxy token issue failed: {exc}") from exc
            checkout_ref = f"{job_id}/repo"
            try:
                text = await self._call_launcher(
                    job_id=job_id,
                    checkout_ref=checkout_ref,
                    proxy_token=proxy_token,
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
            if proxy_token is not None:
                # Finish / cancel / error: best-effort revoke with short wall-clock bound
                # (ARB34-004). Reaper + launcher cancel revoke remain idempotent backstops.
                revoke_budget = finish_revoke_timeout_seconds()
                try:
                    async with asyncio.timeout(revoke_budget):
                        await revoke_job_token(
                            job_id=job_id,
                            client=self._http_client,
                            timeout_seconds=revoke_budget,
                        )
                except TimeoutError:
                    logger.warning(
                        "model-proxy revoke timed out job_id=%s budget_s=%s",
                        job_id,
                        revoke_budget,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("model-proxy revoke failed job_id=%s", job_id, exc_info=True)
            await stop_directory_lease(job_dir)
            # Keep checkout for orchestrator cleanup; remove only our staging copy when safe.
            if remove_job_dir and job_dir.exists() and repo_dir.resolve() != checkout.resolve():
                await asyncio.to_thread(_remove_job_dir, job_dir)

    def _stage_job_files(
        self,
        checkout: Path,
        repo_dir: Path,
        job_dir: Path,
        prompt: str,
        schema_path: Path,
        model: str,
        timeout_seconds: int,
        reasoning_effort: str,
        sandbox: str,
        approval_policy: str,
        allow_unsandboxed: bool,
    ) -> None:
        """Synchronous staging (safe_copytree + writes); always run via ``to_thread``."""
        src = checkout.resolve()
        try:
            src.relative_to(self._checkout_base.resolve())
            if src.name == "repo" and src.parent.parent == self._checkout_base.resolve():
                pass
        except ValueError:
            pass
        safe_copytree(src, repo_dir)
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

    async def _call_launcher(self, *, job_id: str, checkout_ref: str, proxy_token: str, timeout_seconds: int) -> str:
        headers = {
            "Authorization": f"Bearer {self._launcher_token}",
            "Content-Type": "application/json",
        }
        payload = {"job_id": job_id, "checkout_ref": checkout_ref, "proxy_token": proxy_token}
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
            # ARB34-007: surface rolling-upgrade rejects clearly (426 or structured detail).
            if response.status_code == 426 or code == CODEX_UPGRADE_REQUIRED:
                raise CodexRunnerError(
                    CODEX_UPGRADE_REQUIRED,
                    message or "worker too old: upgrade worker, launcher and model-proxy together",
                )
            # Old launcher may answer bare 422 when proxy_token is unknown/required.
            if response.status_code == 422:
                raise CodexRunnerError(
                    CODEX_UPGRADE_REQUIRED,
                    "launcher rejected review request (422); upgrade worker, launcher and model-proxy together",
                )
            allowed = {CODEX_SANDBOX, CODEX_TIMEOUT, CODEX_UPGRADE_REQUIRED}
            raise CodexRunnerError(
                code if code in allowed else CODEX_SANDBOX,
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
