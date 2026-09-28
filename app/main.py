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

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid."""


def _validate_startup_config() -> None:
    """Validate required configuration at startup. Fails fast on misconfiguration."""
    settings = get_settings()

    if not settings.github_webhook_secret:
        raise ConfigurationError(
            "GITHUB_WEBHOOK_SECRET must be set. Webhook signature verification is required for all requests."
        )

    app_config = get_app_config()
    if not app_config.github.allowed_repos:
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
