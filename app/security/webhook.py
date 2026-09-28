"""HMAC verification for GitHub webhooks."""

from __future__ import annotations

import hashlib
import hmac

from app.security.access import extract_presented_key, verify_api_key


def verify_github_signature(*, secret: str, body: bytes, header: str | None) -> bool:
    """Verify X-Hub-Signature-256 HMAC signature.

    Returns True only when:
    - secret is non-empty
    - header is present and has the form 'sha256=<hex>'
    - the computed HMAC matches the provided digest (constant-time comparison)
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


def authorize_webhook(
    *,
    api_key: str,
    webhook_secret: str,
    authorization: str | None,
    x_api_key: str | None,
    body: bytes,
    signature_header: str | None,
) -> bool:
    """Authorize a webhook request.

    Both a valid API key AND a valid HMAC signature are required.
    Missing or invalid signature always results in rejection.
    """
    presented = extract_presented_key(authorization, x_api_key)
    if not verify_api_key(api_key, presented):
        return False
    return verify_github_signature(secret=webhook_secret, body=body, header=signature_header)
