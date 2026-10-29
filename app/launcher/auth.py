"""Shared-token auth between worker and launcher."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.launcher.token import launcher_token, launcher_token_is_configured


def require_launcher_auth(authorization: str | None = Header(default=None)) -> None:
    expected = launcher_token()
    if not launcher_token_is_configured(expected):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="launcher token not configured",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    got = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
