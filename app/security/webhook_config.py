"""Webhook configuration and validation."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_MIN_LENGTH = 16
WEBHOOK_SECRET_PLACEHOLDERS = {"change-me", "changeme", "change_me"}

_webhook_enabled: bool = False


def is_webhook_secret_valid(secret: str) -> tuple[bool, str]:
    """Check if webhook secret is properly configured.

    Returns (is_valid, reason) where reason explains why it's invalid.
    """
    stripped = secret.strip()
    if not stripped:
        return False, "GITHUB_WEBHOOK_SECRET is empty"
    if stripped.lower() in WEBHOOK_SECRET_PLACEHOLDERS:
        return False, f"GITHUB_WEBHOOK_SECRET is set to placeholder '{stripped}'"
    if len(stripped) < WEBHOOK_SECRET_MIN_LENGTH:
        return False, f"GITHUB_WEBHOOK_SECRET is too short ({len(stripped)} chars, minimum {WEBHOOK_SECRET_MIN_LENGTH})"
    return True, ""


def is_webhook_enabled() -> bool:
    """Return True if webhook endpoint is enabled (valid secret configured)."""
    return _webhook_enabled


def set_webhook_enabled(enabled: bool) -> None:
    """Set webhook enabled state. Called during startup validation."""
    global _webhook_enabled
    _webhook_enabled = enabled
