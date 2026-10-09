"""Webhook configuration and validation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.owners.registry import OwnerRegistry

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_MIN_LENGTH = 16
WEBHOOK_SECRET_PLACEHOLDERS = {"change-me", "changeme", "change_me"}

_webhook_enabled: bool = False
_normalized_secret: str = ""
# Multi-owner support: mapping of owner_id -> normalized webhook secret
_owner_webhook_secrets: dict[str, str] = {}


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
        return False, "webhook secret is empty"
    if normalized.lower() in WEBHOOK_SECRET_PLACEHOLDERS:
        return False, f"webhook secret is set to placeholder '{normalized}'"
    if len(normalized) < WEBHOOK_SECRET_MIN_LENGTH:
        return False, (f"webhook secret is too short ({len(normalized)} chars, minimum {WEBHOOK_SECRET_MIN_LENGTH})")
    return True, ""


def is_webhook_enabled() -> bool:
    """Return True if webhook endpoint is enabled (at least one valid secret configured)."""
    return _webhook_enabled


def get_normalized_secret() -> str:
    """Return the normalized webhook secret for HMAC computation (legacy single-owner)."""
    return _normalized_secret


def get_owner_webhook_secrets() -> dict[str, str]:
    """Return mapping of owner_id -> normalized webhook secret for all owners with valid secrets."""
    return _owner_webhook_secrets.copy()


def set_webhook_config(enabled: bool, secret: str) -> None:
    """Set webhook enabled state and normalized secret. Called during startup validation.

    This is the legacy single-owner configuration path.
    """
    global _webhook_enabled, _normalized_secret
    _webhook_enabled = enabled
    _normalized_secret = normalize_webhook_secret(secret) if enabled else ""


def set_multi_owner_webhook_config(registry: OwnerRegistry) -> None:
    """Set webhook configuration from owner registry for multi-owner mode.

    Called during startup validation when owners are configured.
    """
    global _webhook_enabled, _owner_webhook_secrets, _normalized_secret

    _owner_webhook_secrets = {}
    for owner_id, ctx in registry.owners.items():
        if ctx.webhook_secret:
            valid, _ = is_webhook_secret_valid(ctx.webhook_secret)
            if valid:
                _owner_webhook_secrets[owner_id] = normalize_webhook_secret(ctx.webhook_secret)

    # Webhook is enabled if at least one owner has a valid secret
    _webhook_enabled = bool(_owner_webhook_secrets)

    # For backward compatibility, set _normalized_secret to the default owner's secret
    default_ctx = registry.get_default()
    if default_ctx and default_ctx.webhook_secret:
        valid, _ = is_webhook_secret_valid(default_ctx.webhook_secret)
        if valid:
            _normalized_secret = normalize_webhook_secret(default_ctx.webhook_secret)
        else:
            _normalized_secret = ""
    else:
        _normalized_secret = ""
