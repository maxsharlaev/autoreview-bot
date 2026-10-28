"""LAUNCHER_TOKEN helpers (fail closed when unset)."""

from __future__ import annotations

import os

MIN_LAUNCHER_TOKEN_LEN = 16


def launcher_token() -> str:
    return os.environ.get("LAUNCHER_TOKEN", "").strip()


def launcher_token_is_configured(token: str | None = None) -> bool:
    value = launcher_token() if token is None else (token or "").strip()
    return len(value) >= MIN_LAUNCHER_TOKEN_LEN
