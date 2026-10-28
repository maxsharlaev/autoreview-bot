"""Strip secrets before launching Codex."""

from __future__ import annotations

import os
from pathlib import Path

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
