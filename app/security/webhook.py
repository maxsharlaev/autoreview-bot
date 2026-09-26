"""HMAC verification for GitHub webhooks."""

from __future__ import annotations

import hashlib
import hmac

from app.security.access import extract_presented_key, verify_api_key


def verify_github_signature(*, secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header:
        return False
    try:
        algorithm, digest = header.split("=", 1)
    except ValueError:
        return False
    if algorithm != "sha256":
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def authorize_webhook(
    *,
    api_key: str,
    webhook_secret: str,
    authorization: str | None,
    x_api_key: str | None,
    body: bytes,
    signature_header: str | None,
) -> bool:
    presented = extract_presented_key(authorization, x_api_key)
    if not verify_api_key(api_key, presented):
        return False
    if signature_header:
        return verify_github_signature(secret=webhook_secret, body=body, header=signature_header)
    return True
