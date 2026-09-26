"""API access-key checks."""

from __future__ import annotations

import base64
import hmac


def extract_presented_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    token = rest.strip()
    if scheme.lower() == "bearer" and token:
        return token
    if scheme.lower() == "basic" and token:
        try:
            decoded = base64.b64decode(token).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        _user, sep, password = decoded.partition(":")
        if not sep:
            return decoded or None
        return password or _user or None
    return None


def verify_api_key(expected: str, presented: str | None) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)
