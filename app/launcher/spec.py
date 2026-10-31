"""Hard-coded review container create specification (M2).

Workers cannot pass mounts, network, image, or command — only job_id + checkout_ref.
Any change to this module is a security-sensitive review.
"""

from __future__ import annotations

import os
import re
import time
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

# Default path baked into Dockerfile.launcher; override via env for tests/dev only.
# Never taken from the worker HTTP request.
DEFAULT_REVIEW_SECCOMP_PROFILE_PATH = Path("/opt/opr-seccomp/review-bwrap.json")


def review_seccomp_profile_path() -> Path:
    override = os.environ.get("OPR_REVIEW_SECCOMP_PROFILE", "").strip()
    return Path(override) if override else DEFAULT_REVIEW_SECCOMP_PROFILE_PATH


def load_review_seccomp_profile(path: Path | None = None) -> str:
    """Return the seccomp JSON text for SecurityOpt ``seccomp=<json>``.

    Docker Engine expects the profile body (CLI embeds file contents the same way).
    """
    profile_path = path or review_seccomp_profile_path()
    text = profile_path.read_text(encoding="utf-8")
    data = __import__("json").loads(text)
    if not isinstance(data, dict) or "syscalls" not in data:
        raise RuntimeError(f"invalid seccomp profile at {profile_path}")
    # Compact JSON keeps HostConfig smaller; preserve key order from file via loads/dumps.
    return __import__("json").dumps(data, separators=(",", ":"))


DEFAULT_APPARMOR_PROFILE = "unconfined"
# Docker AppArmor profile name: "unconfined" or a profile name (not a host path).
_APPARMOR_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,126}$")


def validate_apparmor_profile(value: str) -> str:
    """Return a safe AppArmor profile name or raise (fail closed)."""
    name = (value or "").strip()
    if not name:
        raise RuntimeError("LAUNCHER_APPARMOR_PROFILE must not be empty")
    if name == "unconfined":
        return name
    if "/" in name or chr(92) in name or ".." in name or name.startswith("."):
        raise RuntimeError(f"invalid LAUNCHER_APPARMOR_PROFILE: {value!r}")
    if not _APPARMOR_PROFILE_RE.fullmatch(name):
        raise RuntimeError(f"invalid LAUNCHER_APPARMOR_PROFILE: {value!r}")
    return name


def review_apparmor_profile() -> str:
    """Operator-trusted AppArmor profile for review containers (never from the HTTP request)."""
    raw = os.environ.get("LAUNCHER_APPARMOR_PROFILE", DEFAULT_APPARMOR_PROFILE)
    return validate_apparmor_profile(raw if raw is not None else DEFAULT_APPARMOR_PROFILE)


def review_security_opt(*, seccomp_json: str | None = None, apparmor_profile: str | None = None) -> list[str]:
    """Hard-coded security options for review containers.

    Seccomp must never be unconfined (narrow baked profile). AppArmor defaults to
    ``unconfined`` so bubblewrap can mount on Linux hosts that enforce Docker's
    default AppArmor profile; operators may set ``LAUNCHER_APPARMOR_PROFILE`` to a
    dedicated profile name when one exists.
    """
    profile = seccomp_json if seccomp_json is not None else load_review_seccomp_profile()
    if "unconfined" in profile.lower():
        raise RuntimeError("review seccomp profile must not be unconfined")
    apparmor = (
        validate_apparmor_profile(apparmor_profile) if apparmor_profile is not None else review_apparmor_profile()
    )
    return [
        "no-new-privileges:true",
        f"seccomp={profile}",
        f"apparmor={apparmor}",
    ]


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


def checkout_volume_name() -> str:
    """Named volume that holds staged checkouts (daemon-side; never a launcher-local bind)."""
    return os.environ.get("OPR_CHECKOUT_VOLUME", "opr_checkouts").strip() or "opr_checkouts"


def _volume_mount(
    *,
    volume_name: str,
    subpath: str,
    target: str,
    read_only: bool,
) -> dict[str, Any]:
    """Docker Mount using Type=volume + VolumeOptions.Subpath (Engine >= 26 / API >= 1.45)."""
    rel = (subpath or "").strip().replace(chr(92), "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        raise RuntimeError(f"invalid volume subpath: {subpath!r}")
    return {
        "Type": "volume",
        "Source": volume_name,
        "Target": target,
        "ReadOnly": read_only,
        "VolumeOptions": {"Subpath": rel},
    }


def build_host_config(
    *,
    checkout_subpath: str,
    work_subpath: str,
    prompt_subpath: str,
    schema_subpath: str,
    volume_name: str | None = None,
    memory: str = DEFAULT_MEMORY,
    nano_cpus: int = DEFAULT_NANO_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
) -> dict[str, Any]:
    """Docker HostConfig: volume Subpath mounts (not launcher-local binds), caps dropped.

    Bind sources that are paths inside the launcher container are wrong: the Docker
    daemon resolves binds on the *host*. Staged files live on the named volume
    ``opr_checkouts``; mount per-job subpaths via VolumeOptions.Subpath instead.
    """
    vol = (volume_name or checkout_volume_name()).strip() or checkout_volume_name()
    mounts = [
        _volume_mount(volume_name=vol, subpath=checkout_subpath, target="/checkout", read_only=True),
        _volume_mount(volume_name=vol, subpath=prompt_subpath, target="/job/prompt.txt", read_only=True),
        _volume_mount(volume_name=vol, subpath=schema_subpath, target="/job/schema.json", read_only=True),
        _volume_mount(volume_name=vol, subpath=work_subpath, target="/work", read_only=False),
    ]
    return {
        "Mounts": mounts,
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": review_security_opt(),
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
    proxy_token: str,
) -> dict[str, Any]:
    """Full ContainerCreate body. Command/entrypoint come from the image.

    ``proxy_token`` is the short-lived ``opr_job_`` bearer (never a real model key).
    Only ``CODEX_API_KEY`` is set; entrypoint may mirror it to ``OPENAI_API_KEY``.
    """
    env = [
        f"CODEX_API_KEY={proxy_token}",
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
            # Unix creation time for revoke retry window (ARB34-009); Docker
            # Created is also used as a fallback by the reaper.
            "opr.created_unix": str(int(time.time())),
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
    if binds:
        raise RuntimeError("review containers must use volume Mounts, not Binds")
    mounts = host_config.get("Mounts") or []
    if not mounts:
        raise RuntimeError("review container Mounts required")
    checkout_ro = False
    for mount in mounts:
        if not isinstance(mount, dict):
            raise RuntimeError("invalid mount entry")
        src = str(mount.get("Source") or "")
        target = str(mount.get("Target") or "")
        mtype = str(mount.get("Type") or "").lower()
        lower_src = src.lower()
        if "docker.sock" in lower_src or "/var/run/docker" in lower_src:
            raise RuntimeError("review container must never mount the docker socket")
        if "docker.sock" in target.lower():
            raise RuntimeError("review container must never mount the docker socket")
        if mtype != "volume":
            raise RuntimeError("review mounts must be Type=volume (daemon Subpath)")
        vol_opts = mount.get("VolumeOptions") or {}
        if not isinstance(vol_opts, dict) or not str(vol_opts.get("Subpath") or "").strip():
            raise RuntimeError("volume mount requires VolumeOptions.Subpath")
        sub = str(vol_opts.get("Subpath")).replace(chr(92), "/")
        if sub.startswith("/") or ".." in sub.split("/"):
            raise RuntimeError("VolumeOptions.Subpath must be a safe relative path")
        if target == "/checkout":
            if not mount.get("ReadOnly", False):
                raise RuntimeError("checkout mount must be read-only")
            checkout_ro = True
    if not checkout_ro:
        raise RuntimeError("RO /checkout volume mount required")
    if "ALL" not in (host_config.get("CapDrop") or []):
        raise RuntimeError("CapDrop ALL required")
    if host_config.get("Privileged"):
        raise RuntimeError("privileged review containers are forbidden")
    security = [str(s) for s in (host_config.get("SecurityOpt") or [])]
    if not any("no-new-privileges" in s for s in security):
        raise RuntimeError("no-new-privileges required")
    if any("seccomp=unconfined" in s.lower() for s in security):
        raise RuntimeError("seccomp=unconfined is forbidden for review containers")
    seccomp_opts = [s for s in security if s.startswith("seccomp=")]
    if len(seccomp_opts) != 1:
        raise RuntimeError("exactly one seccomp= SecurityOpt required")
    body = seccomp_opts[0].removeprefix("seccomp=")
    if not body.lstrip().startswith("{"):
        raise RuntimeError("seccomp SecurityOpt must embed the profile JSON body")
    if '"syscalls"' not in body and "'syscalls'" not in body:
        raise RuntimeError("seccomp profile JSON missing syscalls")
    apparmor_opts = [s for s in security if s.startswith("apparmor=")]
    if len(apparmor_opts) != 1:
        raise RuntimeError("exactly one apparmor= SecurityOpt required")
    validate_apparmor_profile(apparmor_opts[0].removeprefix("apparmor="))


def _parse_memory_bytes(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("g"):
        return int(float(text[:-1]) * 1024**3)
    if text.endswith("m"):
        return int(float(text[:-1]) * 1024**2)
    if text.endswith("k"):
        return int(float(text[:-1]) * 1024)
    return int(text)
