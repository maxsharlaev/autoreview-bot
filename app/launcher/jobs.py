"""Run one review container from the hard-coded spec."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from app.launcher.docker_api import DockerApiError, DockerClient
from app.launcher.paths import CheckoutPathError, job_work_dir, resolve_checkout, validate_job_id
from app.launcher.spec import (
    assert_safe_host_config,
    build_container_config,
    build_host_config,
    container_name,
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
        checkout, prompt, schema, work, out_file = stage_paths(job_id, checkout_ref)
    except CheckoutPathError as exc:
        raise LauncherJobError("CODEX_SANDBOX", str(exc)) from exc

    job_dir = prompt.parent
    meta = read_job_meta(job_dir)
    model = str(meta.get("model") or os.environ.get("OPR_DEFAULT_MODEL") or "gpt-5.6-sol")
    timeout_seconds = int(meta.get("timeout_seconds") or 600)
    reasoning_effort = str(meta.get("reasoning_effort") or "medium")
    sandbox = str(meta.get("sandbox") or "read-only")
    approval_policy = str(meta.get("approval_policy") or "never")
    allow_unsandboxed = bool(meta.get("allow_unsandboxed") or False)

    host_config = build_host_config(
        checkout=checkout,
        work_dir=work,
        prompt=prompt,
        schema=schema,
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
    # Defense in depth: never put docker.sock into Env or Binds after build.
    assert_safe_host_config(config["HostConfig"])

    name = container_name(job_id)
    owns = client is None
    docker = client or DockerClient()
    container_id = ""
    started = time.monotonic()
    try:
        try:
            container_id = docker.create_container(name, config)
            docker.start_container(container_id)
            status_code = docker.wait_container(container_id, timeout_seconds=timeout_seconds + 30)
        except DockerApiError as exc:
            raise LauncherJobError("CODEX_SANDBOX", f"docker error: {exc}") from exc

        logs = ""
        try:
            logs = docker.logs(container_id)
        except DockerApiError:
            logs = ""

        if status_code != 0:
            snippet = logs.strip().splitlines()[-1] if logs.strip() else f"exit {status_code}"
            # Sandbox probe failures use entrypoint exit 43.
            if status_code == 43:
                raise LauncherJobError(
                    "CODEX_SANDBOX",
                    f"linux sandbox unavailable inside review container: {snippet[:240]}",
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
        if owns:
            docker.close()
