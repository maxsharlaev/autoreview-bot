"""Hard-coded review container create specification (M2).

Workers cannot pass mounts, network, image, or command — only job_id + checkout_ref.
Any change to this module is a security-sensitive review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Image built from Dockerfile.review; override only via launcher env (operator-trusted).
DEFAULT_REVIEW_IMAGE = "opr-review:local"
REVIEW_LABEL = "opr.review"
REVIEW_LABEL_VALUE = "1"
DEFAULT_NETWORK = "autoreview_review_egress"
DEFAULT_MEMORY = "1g"
DEFAULT_NANO_CPUS = 1_000_000_000  # 1 CPU
DEFAULT_PIDS_LIMIT = 256
DEFAULT_TMPFS_SIZE = "64m"


@dataclass(frozen=True)
class ReviewJobFiles:
    """Paths staged by the worker under the shared checkout volume."""

    checkout: Path
    prompt: Path
    schema: Path
    work_dir: Path
    out_file: Path


def review_image() -> str:
    return os.environ.get("OPR_REVIEW_IMAGE", DEFAULT_REVIEW_IMAGE).strip() or DEFAULT_REVIEW_IMAGE


def review_network() -> str:
    return os.environ.get("OPR_REVIEW_NETWORK", DEFAULT_NETWORK).strip() or DEFAULT_NETWORK


def container_name(job_id: str) -> str:
    return f"opr-review-{job_id}"


def build_host_config(
    *,
    checkout: Path,
    work_dir: Path,
    prompt: Path,
    schema: Path,
    memory: str = DEFAULT_MEMORY,
    nano_cpus: int = DEFAULT_NANO_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
) -> dict[str, Any]:
    """Docker HostConfig body: caps dropped, no privileges, RO checkout, no socket."""
    # Bind mounts: checkout RO, job files RO, work RW. Never mount docker.sock.
    binds = [
        f"{checkout}:/checkout:ro",
        f"{prompt}:/job/prompt.txt:ro",
        f"{schema}:/job/schema.json:ro",
        f"{work_dir}:/work:rw",
    ]
    return {
        "Binds": binds,
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": [
            "no-new-privileges:true",
            # Same as the worker Compose service: Docker's default seccomp/AppArmor
            # block bubblewrap (CLONE_NEWUSER). Codex still sandboxes with bwrap.
            "seccomp=unconfined",
            "apparmor=unconfined",
        ],
        "Memory": _parse_memory_bytes(memory),
        "NanoCpus": nano_cpus,
        "PidsLimit": pids_limit,
        "NetworkMode": review_network(),
        "AutoRemove": False,  # launcher removes explicitly after wait (also reaper)
        "ReadonlyRootfs": False,  # Codex/npm need some write under HOME; /checkout is RO
        "Tmpfs": {"/tmp": f"size={DEFAULT_TMPFS_SIZE},mode=1777"},
        "ExtraHosts": [],
    }


def build_container_config(
    *,
    job_id: str,
    host_config: dict[str, Any],
    model: str,
    timeout_seconds: int,
    reasoning_effort: str,
    sandbox: str,
    approval_policy: str,
    allow_unsandboxed: bool,
    model_api_key: str,
) -> dict[str, Any]:
    """Full ContainerCreate body. Command/entrypoint come from the image."""
    env = [
        f"CODEX_API_KEY={model_api_key}",
        f"OPENAI_API_KEY={model_api_key}",
        f"OPR_MODEL={model}",
        f"OPR_TIMEOUT_SECONDS={int(timeout_seconds)}",
        f"OPR_REASONING_EFFORT={reasoning_effort}",
        f"OPR_SANDBOX={sandbox}",
        f"OPR_APPROVAL_POLICY={approval_policy}",
        f"OPR_ALLOW_UNSANDBOXED={'1' if allow_unsandboxed else '0'}",
        "LANG=C.UTF-8",
        "TERM=dumb",
        "HOME=/home/review",
    ]
    return {
        "Image": review_image(),
        "Env": env,
        "Labels": {
            REVIEW_LABEL: REVIEW_LABEL_VALUE,
            "opr.job_id": job_id,
        },
        "HostConfig": host_config,
        "User": "1000:1000",
        "WorkingDir": "/checkout",
        "NetworkDisabled": False,
        "AttachStdin": False,
        "AttachStdout": True,
        "AttachStderr": True,
    }


def assert_safe_host_config(host_config: dict[str, Any]) -> None:
    """Fail closed if a future edit accidentally weakens the hard-coded spec."""
    binds = host_config.get("Binds") or []
    for bind in binds:
        lower = bind.lower()
        if "docker.sock" in lower or "/var/run/docker" in lower:
            raise RuntimeError("review container must never mount the docker socket")
        if bind.rstrip().endswith(":/checkout:rw") or ":/checkout:rw" in bind:
            raise RuntimeError("checkout mount must be read-only")
    if "ALL" not in (host_config.get("CapDrop") or []):
        raise RuntimeError("CapDrop ALL required")
    if host_config.get("Privileged"):
        raise RuntimeError("privileged review containers are forbidden")
    security = host_config.get("SecurityOpt") or []
    if not any("no-new-privileges" in str(s) for s in security):
        raise RuntimeError("no-new-privileges required")


def _parse_memory_bytes(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("g"):
        return int(float(text[:-1]) * 1024**3)
    if text.endswith("m"):
        return int(float(text[:-1]) * 1024**2)
    if text.endswith("k"):
        return int(float(text[:-1]) * 1024)
    return int(text)
