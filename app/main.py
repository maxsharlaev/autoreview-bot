from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.config import get_app_config, get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.owners.registry import OwnerConfigError, OwnerRegistry
from app.queue import create_redis_pool
from app.security.webhook_config import (
    WEBHOOK_SECRET_MIN_LENGTH,
    is_webhook_secret_valid,
    set_multi_owner_webhook_config,
    set_webhook_config,
)

logger = logging.getLogger(__name__)


def _validate_startup_config() -> None:
    """Validate configuration at startup. Logs warnings for missing optional config."""
    settings = get_settings()
    app_config = get_app_config()

    # Build and validate owner registry (M1: validates config, logs warnings)
    try:
        registry = OwnerRegistry.build(settings, app_config)
        for warning in registry.warnings:
            logger.warning("Owner config: %s", warning)
        if registry.owners and registry.legacy_default_alias() is None:
            logger.warning(
                "Owner config: no owner is bound to legacy owner_id 'default'. Findings, review history "
                "and comment authors recorded before multi-owner support stay hidden from every owner. "
                "Add `aliases: [default]` to the owner that should inherit them."
            )

        owner_ids = list(registry.owners.keys())
        if not owner_ids:
            logger.info("Owner registry: legacy mode, no GitHub credentials")
        elif len(owner_ids) == 1 and owner_ids[0] == "default":
            logger.info("Owner registry: legacy single-owner mode (owner=default)")
        else:
            logger.info(
                "Owner registry: %d owner(s) configured, default=%s",
                len(owner_ids),
                registry.default_owner_id,
            )
    except OwnerConfigError as exc:
        logger.error("Owner configuration error: %s", exc)
        raise SystemExit(1) from exc

    # Multi-owner webhook configuration
    set_multi_owner_webhook_config(registry)

    # Check if any owner has a valid webhook secret
    webhook_secrets = registry.webhook_secrets()
    if not webhook_secrets:
        # No valid secrets - check legacy config for backward compatibility
        valid, reason = is_webhook_secret_valid(settings.github_webhook_secret)
        if not valid:
            set_webhook_config(enabled=False, secret="")
            logger.warning(
                "%s. Webhook endpoint (POST /api/v1/pull-request) is disabled. "
                "To enable, set GITHUB_WEBHOOK_SECRET to a random value of at least %d characters "
                "(e.g. openssl rand -hex 32). The /api/v1/reviews endpoint remains available.",
                reason,
                WEBHOOK_SECRET_MIN_LENGTH,
            )
        else:
            set_webhook_config(enabled=True, secret=settings.github_webhook_secret)

    if webhook_secrets and not app_config.github.allowed_repos:
        logger.warning(
            "github.allowed_repos is empty: webhook accepts requests from any repository. "
            "Configure an explicit allowlist for production use."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level, get_settings().log_format)
    _validate_startup_config()
    engine = create_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = await create_redis_pool()
    logger.info("api ready")
    yield
    await app.state.redis.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(title="Open PR Review", version="0.1.0", lifespan=lifespan)
    application.include_router(api_router)
    return application


app = create_app()
