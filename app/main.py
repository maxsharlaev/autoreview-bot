from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.config import get_app_config, get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.queue import create_redis_pool
from app.security.webhook_config import (
    WEBHOOK_SECRET_MIN_LENGTH,
    is_webhook_secret_valid,
    set_webhook_config,
)

logger = logging.getLogger(__name__)


def _validate_startup_config() -> None:
    """Validate configuration at startup. Logs warnings for missing optional config."""
    settings = get_settings()

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

    app_config = get_app_config()
    if valid and not app_config.github.allowed_repos:
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
