from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.queue import create_redis_pool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level, get_settings().log_format)
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
