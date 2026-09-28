"""Webhook configuration and validation."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_MIN_LENGTH = 16
WEBHOOK_SECRET_PLACEHOLDERS = {"change-me", "changeme", "change_me"}

_webhook_enabled: bool = False
_normalized_secret: str = ""


def normalize_webhook_secret(secret: str) -> str:
    """Normalize webhook secret by stripping whitespace.

    This normalized value is used consistently for both validation and HMAC computation.
    """
    return secret.strip()


def is_webhook_secret_valid(secret: str) -> tuple[bool, str]:
    """Check if webhook secret is properly configured.

    Returns (is_valid, reason) where reason explains why it's invalid.
    """
    normalized = normalize_webhook_secret(secret)
    if not normalized:
        return False, "GITHUB_WEBHOOK_SECRET is empty"
    if normalized.lower() in WEBHOOK_SECRET_PLACEHOLDERS:
        return False, f"GITHUB_WEBHOOK_SECRET is set to placeholder '{normalized}'"
    if len(normalized) < WEBHOOK_SECRET_MIN_LENGTH:
        return False, (
            f"GITHUB_WEBHOOK_SECRET is too short ({len(normalized)} chars, minimum {WEBHOOK_SECRET_MIN_LENGTH})"
        )
    return True, ""


def is_webhook_enabled() -> bool:
    """Return True if webhook endpoint is enabled (valid secret configured)."""
    return _webhook_enabled


def get_normalized_secret() -> str:
    """Return the normalized webhook secret for HMAC computation."""
    return _normalized_secret


def set_webhook_config(enabled: bool, secret: str) -> None:
    """Set webhook enabled state and normalized secret. Called during startup validation."""
    global _webhook_enabled, _normalized_secret
    _webhook_enabled = enabled
    _normalized_secret = normalize_webhook_secret(secret) if enabled else ""
