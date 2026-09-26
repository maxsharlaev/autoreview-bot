"""Strip secrets before launching Codex."""

from __future__ import annotations

import os

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


def sandbox_env(openai_api_key: str) -> dict[str, str]:
    """Codex exec authenticates with CODEX_API_KEY; OPENAI_API_KEY alone is ignored."""
    key = openai_api_key.strip()
    env = {name: value for name, value in os.environ.items() if name in ALLOWED_ENV}
    env["OPENAI_API_KEY"] = key
    env["CODEX_API_KEY"] = key
    env.setdefault("HOME", os.path.expanduser("~"))
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    return env
