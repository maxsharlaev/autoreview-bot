"""HMAC verification for GitHub webhooks."""

from __future__ import annotations

import hashlib
import hmac


def verify_github_signature(*, secret: str, body: bytes, header: str | None) -> bool:
    """Verify X-Hub-Signature-256 HMAC signature.

    Returns True only when:
    - secret is non-empty
    - header is present and has the form 'sha256=<hex>'
    - header contains only ASCII characters
    - the computed HMAC matches the provided digest (constant-time comparison)

    This is the ONLY authentication required for the webhook endpoint.
    GitHub webhooks cannot send custom API key headers.
    """
    if not secret or not header:
        return False
    # Reject non-ASCII characters in signature header
    if not header.isascii():
        return False
    try:
        algorithm, digest = header.split("=", 1)
    except ValueError:
        return False
    if algorithm != "sha256":
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def verify_github_signature_multi(
    *,
    secrets: dict[str, str],
    body: bytes,
    header: str | None,
) -> set[str]:
    """Verify X-Hub-Signature-256 against multiple owner secrets.

    Performs constant-time comparison against ALL secrets (no early exit)
    to prevent timing attacks that could reveal which secrets are valid.

    Args:
        secrets: Mapping of owner_id -> webhook_secret
        body: Request body bytes
        header: X-Hub-Signature-256 header value

    Returns:
        Set of owner_ids whose secrets matched the signature.
        Empty set if no secrets matched or header is invalid.
    """
    if not header:
        return set()

    # Reject non-ASCII characters in signature header
    if not header.isascii():
        return set()

    try:
        algorithm, digest = header.split("=", 1)
    except ValueError:
        return set()

    if algorithm != "sha256":
        return set()

    matched_owners: set[str] = set()

    # Deduplicate secrets by value to avoid redundant computations
    # but still iterate all owners for constant-time behavior
    secret_to_owners: dict[str, list[str]] = {}
    for owner_id, secret in secrets.items():
        if secret:
            secret_to_owners.setdefault(secret, []).append(owner_id)

    for secret, owner_ids in secret_to_owners.items():
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, digest):
            matched_owners.update(owner_ids)

    return matched_owners
