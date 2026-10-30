"""Run one review container from the hard-coded spec."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from app.launcher.docker_api import DockerApiError, DockerClient
from app.launcher.job_lifecycle import lifecycle
from app.launcher.paths import CheckoutPathError, job_work_dir, resolve_checkout, validate_job_id
from app.launcher.spec import (
    assert_safe_host_config,
    build_container_config,
    build_host_config,
    checkout_volume_name,
    container_name,
)
from app.launcher.timeouts import (
    CONTAINER_WAIT_GRACE_SECONDS,
    launcher_cancel_deadline_seconds,
    launcher_cancel_wait_seconds,
    max_review_timeout_seconds,
    validate_checkout_orphan_ttl,
)

logger = logging.getLogger(__name__)


class LauncherJobError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def checkout_base() -> Path:
    raw = os.environ.get("OPR_CHECKOUT_BASE", "/var/opr/checkouts").strip()
    return Path(raw)


def stage_paths(job_id: str, checkout_ref: str) -> tuple[Path, Path, Path, Path, Path]:
    """Resolve checkout + expected staged prompt/schema/work/out under the base."""
    jid = validate_job_id(job_id)
    base = checkout_base()
    checkout = resolve_checkout(base, checkout_ref)
    # Prompt/schema are staged by the worker next to the checkout under the job dir.
    # Preferred layout: {base}/{job_id}/repo  + {base}/{job_id}/prompt.txt + schema.json
    job_dir = checkout.parent if checkout.name == "repo" else checkout
    prompt = job_dir / "prompt.txt"
    schema = job_dir / "schema.json"
    if not prompt.is_file():
        # Fallback: files written inside the checkout by older staging.
        prompt = checkout / ".open-pr-review-prompt.txt"
    if not schema.is_file():
        schema = checkout / ".open-pr-review-schema.json"
    if not prompt.is_file() or not schema.is_file():
        raise LauncherJobError("CODEX_SANDBOX", "staged prompt/schema missing for job")
    work = job_work_dir(base, jid)
    work.mkdir(parents=True, exist_ok=True)
    # Review containers run as uid 1000; volume dirs created here are root-owned.
    try:
        os.chmod(work, 0o777)
    except OSError:
        pass
    out_file = work / "out.json"
    if out_file.exists():
        out_file.unlink()
    return checkout, prompt, schema, work, out_file


def read_job_meta(job_dir: Path) -> dict:
    import json

    meta_path = job_dir / "job.json"
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _volume_subpath(base: Path, path: Path) -> str:
    """Return a POSIX-relative subpath of ``path`` under ``base`` for VolumeOptions.Subpath."""
    rel = path.resolve().relative_to(base.resolve())
    text = rel.as_posix()
    if not text or text.startswith("/") or ".." in text.split("/"):
        raise LauncherJobError("CODEX_SANDBOX", "staged path escapes checkout volume")
    return text


def run_review_job(
    *,
    job_id: str,
    checkout_ref: str,
    model_api_key: str,
    client: DockerClient | None = None,
) -> dict:
    """Create, start, wait, remove a review container. Returns result metadata."""
    if not (model_api_key or "").strip():
        raise LauncherJobError("CODEX_SANDBOX", "model api key missing")
    try:
        lifecycle.accept(job_id)
    except RuntimeError as exc:
        raise LauncherJobError("CODEX_SANDBOX", str(exc)) from exc

    cancelled = False
    container_id = ""
    owns = client is None
    docker = client or DockerClient()
    started = time.monotonic()
    try:
        try:
            checkout, prompt, schema, work, out_file = stage_paths(job_id, checkout_ref)
        except CheckoutPathError as exc:
            raise LauncherJobError("CODEX_SANDBOX", str(exc)) from exc

        job_dir = prompt.parent
        meta = read_job_meta(job_dir)
        model = str(meta.get("model") or os.environ.get("OPR_DEFAULT_MODEL") or "gpt-5.6-sol")
        timeout_seconds = int(meta.get("timeout_seconds") or 600)
        max_timeout = max_review_timeout_seconds()
        if timeout_seconds > max_timeout:
            raise LauncherJobError(
                "CODEX_SANDBOX",
                f"timeout_seconds ({timeout_seconds}) exceeds OPR_MAX_REVIEW_TIMEOUT_SECONDS ({max_timeout})",
            )
        try:
            validate_checkout_orphan_ttl(timeout_seconds=timeout_seconds)
        except RuntimeError as exc:
            raise LauncherJobError("CODEX_SANDBOX", str(exc)) from exc
        reasoning_effort = str(meta.get("reasoning_effort") or "medium")
        sandbox = str(meta.get("sandbox") or "read-only")
        approval_policy = str(meta.get("approval_policy") or "never")
        allow_unsandboxed = bool(meta.get("allow_unsandboxed") or False)

        base = checkout_base()
        host_config = build_host_config(
            volume_name=checkout_volume_name(),
            checkout_subpath=_volume_subpath(base, checkout),
            work_subpath=_volume_subpath(base, work),
            prompt_subpath=_volume_subpath(base, prompt),
            schema_subpath=_volume_subpath(base, schema),
        )
        assert_safe_host_config(host_config)
        config = build_container_config(
            job_id=job_id,
            host_config=host_config,
            model=model,
            timeout_seconds=timeout_seconds,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            approval_policy=approval_policy,
            allow_unsandboxed=allow_unsandboxed,
            model_api_key=model_api_key.strip(),
        )
        assert_safe_host_config(config["HostConfig"])

        name = container_name(job_id)
        try:
            lifecycle.check_not_cancelled(job_id)
            lifecycle.mark_creating(job_id)
            container_id = docker.create_container(name, config)
            lifecycle.check_not_cancelled(job_id)
            docker.start_container(container_id)
            lifecycle.mark_running(job_id, container_id)
            status_code = docker.wait_container(
                container_id, timeout_seconds=timeout_seconds + CONTAINER_WAIT_GRACE_SECONDS
            )
        except DockerApiError as exc:
            body = (getattr(exc, "body", None) or str(exc)).lower()
            code = getattr(exc, "status_code", None)
            if code == 404 or "no such container" in body or "not found" in body:
                raise LauncherJobError(
                    "CODEX_SANDBOX",
                    "review container removed during wait (cancelled or reaped)",
                ) from exc
            raise LauncherJobError("CODEX_SANDBOX", f"docker error: {exc}") from exc
        except RuntimeError as exc:
            # Lifecycle cancel signals (DockerApiError is also a RuntimeError — handled above).
            cancelled = "cancelled" in str(exc).lower()
            if cancelled:
                raise LauncherJobError("CODEX_SANDBOX", f"review cancelled: {exc}") from exc
            raise LauncherJobError("CODEX_SANDBOX", str(exc)) from exc

        logs = ""
        try:
            logs = docker.logs(container_id)
        except DockerApiError:
            logs = ""

        if status_code != 0:
            snippet = logs.strip().splitlines()[-1] if logs.strip() else f"exit {status_code}"
            if status_code == 43:
                raise LauncherJobError(
                    "CODEX_SANDBOX",
                    f"linux sandbox unavailable inside review container: {snippet[:240]}",
                )
            if status_code == 44:
                # entrypoint.sh: timeout --signal=KILL -> exit 44
                raise LauncherJobError(
                    "CODEX_TIMEOUT",
                    f"review container timed out: {snippet[:240]}",
                )
            raise LauncherJobError(
                "CODEX_SANDBOX",
                f"review container exit {status_code}: {snippet[:240]}",
            )
        if not out_file.is_file():
            raise LauncherJobError("CODEX_SANDBOX", "review container produced no output file")
        text = out_file.read_text(encoding="utf-8")
        return {
            "job_id": job_id,
            "status": "ok",
            "output_path": str(out_file),
            "output": text,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        if container_id:
            try:
                docker.remove_container(container_id, force=True)
            except DockerApiError as exc:
                logger.warning("failed to remove review container %s: %s", container_id[:12], exc)
        lifecycle.mark_finished(job_id, cancelled=cancelled or lifecycle.is_cancelled(job_id))
        if owns:
            docker.close()


def _remaining_cancel_budget(deadline_monotonic: float, *, floor: float = 0.05) -> float:
    """Seconds left before the overall cancel deadline (never below ``floor`` while positive)."""
    left = deadline_monotonic - time.monotonic()
    if left <= 0:
        return 0.0
    return max(floor, left)


def _force_remove_job_containers(
    docker: DockerClient,
    jid: str,
    *,
    deadline_monotonic: float,
) -> int:
    """Kill + force-remove all containers labeled for ``jid``. Returns removals count.

    Each Docker call uses only the remaining overall cancel budget so list/kill/remove
    cannot run to the client default (60 s) under a slow daemon.
    """
    removed = 0
    budget = _remaining_cancel_budget(deadline_monotonic)
    if budget <= 0:
        return 0
    try:
        containers = docker.list_containers_for_job(jid, timeout=budget)
    except DockerApiError as exc:
        raise LauncherJobError("CODEX_SANDBOX", f"docker list failed: {exc}") from exc
    for item in containers:
        cid = str(item.get("Id") or "")
        if not cid:
            continue
        budget = _remaining_cancel_budget(deadline_monotonic)
        if budget <= 0:
            break
        try:
            docker.kill_container(cid, timeout=budget)
        except DockerApiError:
            pass
        budget = _remaining_cancel_budget(deadline_monotonic)
        if budget <= 0:
            break
        try:
            docker.remove_container(cid, force=True, timeout=budget)
            removed += 1
        except DockerApiError as exc:
            logger.warning("cancel remove failed job=%s cid=%s: %s", jid, cid[:12], exc)
    return removed


def cancel_review_job(*, job_id: str, client: DockerClient | None = None) -> dict:
    """Force-remove review containers for ``job_id`` and coordinate with in-flight launches.

    Marks the job cancelled (tombstone if unknown), **immediately** kills any visible
    containers, then waits briefly for an in-flight launch to reach a terminal state,
    and removes again. ``confirmed`` is True only when no container remains and no
    launch is active.

    The whole operation (Docker list/kill/remove + lifecycle wait) shares one deadline
    ``LAUNCHER_CANCEL_DEADLINE_SECONDS`` (< worker DELETE read timeout). Each Docker
    call receives only the remaining budget.
    """
    from app.launcher.paths import validate_uuid_job_id

    jid = validate_uuid_job_id(job_id)
    lifecycle.request_cancel(jid)

    owns = client is None
    docker = client or DockerClient()
    removed = 0
    deadline = time.monotonic() + launcher_cancel_deadline_seconds()
    try:
        # Kill first so a blocked wait_container can finish and mark_finished promptly.
        removed += _force_remove_job_containers(docker, jid, deadline_monotonic=deadline)
        wait_budget = min(launcher_cancel_wait_seconds(), _remaining_cancel_budget(deadline, floor=0.0))
        if wait_budget > 0:
            lifecycle.wait_until_launch_idle(jid, timeout=wait_budget)
        removed += _force_remove_job_containers(docker, jid, deadline_monotonic=deadline)

        leftover: list = ["?"]
        budget = _remaining_cancel_budget(deadline)
        if budget > 0:
            try:
                leftover = docker.list_containers_for_job(jid, timeout=budget)
            except DockerApiError:
                leftover = ["?"]
        confirmed = (not leftover) and (not lifecycle.launch_active(jid))
        return {"job_id": jid, "removed": removed, "confirmed": confirmed}
    finally:
        if owns:
            docker.close()
