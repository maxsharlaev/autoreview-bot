"""HMAC verification for GitHub webhooks."""

from __future__ import annotations

import hashlib
import hmac


def verify_github_signature(*, secret: str, body: bytes, header: str | None) -> bool:
    """Verify X-Hub-Signature-256 HMAC signature.

    Returns True only when:
    - secret is non-empty
    - header is present and has the form 'sha256=<hex>'
    - the computed HMAC matches the provided digest (constant-time comparison)

    This is the ONLY authentication required for the webhook endpoint.
    GitHub webhooks cannot send custom API key headers.
    """
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
