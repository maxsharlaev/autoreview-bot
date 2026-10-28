"""Strip secrets before launching Codex."""

from __future__ import annotations

import os
from pathlib import Path

SAFE_SANDBOX_MODE = "read-only"


def validate_review_sandbox(sandbox: str, *, allow_unsandboxed: bool) -> str:
    """Return the sandbox mode, or raise ValueError when it is unsafe for public runs.

    When ``allow_unsandboxed`` is false (default), only ``read-only`` is accepted so
    ``codex exec`` cannot be pointed at ``danger-full-access`` / ``workspace-write``
    while the fail-closed preflight still passes. The escape hatch must be set
    explicitly for trusted private/dev.
    """
    mode = (sandbox or "").strip()
    if not mode:
        raise ValueError("codex.sandbox must not be empty")
    if allow_unsandboxed:
        return mode
    if mode != SAFE_SANDBOX_MODE:
        raise ValueError(
            f"codex.sandbox={mode!r} is not allowed when codex.allow_unsandboxed is false; "
            f"only {SAFE_SANDBOX_MODE!r} is accepted "
            "(set allow_unsandboxed=true only for trusted private/dev)"
        )
    return mode


ALLOWED_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CODEX_API_KEY",
    "CODEX_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
}


def sandbox_env(openai_api_key: str, *, codex_home: Path | None = None) -> dict[str, str]:
    """Codex exec authenticates with CODEX_API_KEY; OPENAI_API_KEY alone is ignored.

    When ``codex_home`` is set, it is exported as ``CODEX_HOME`` so Codex loads the
    per-run ``shell_environment_policy`` and execpolicy rules prepared for the review
    agent. That policy strips the model key from *agent-spawned* child processes;
    Codex itself still needs the key in this process environment.
    """
    key = openai_api_key.strip()
    env = {name: value for name, value in os.environ.items() if name in ALLOWED_ENV}
    env["OPENAI_API_KEY"] = key
    env["CODEX_API_KEY"] = key
    env.setdefault("HOME", os.path.expanduser("~"))
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env
