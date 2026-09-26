from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import collect_runtime_gauges

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    factory = getattr(request.app.state, "session_factory", None)
    redis = getattr(request.app.state, "redis", None)
    if factory is not None and redis is not None:
        try:
            await collect_runtime_gauges(factory, redis)
        except Exception:
            logger.exception("metrics scrape collection failed")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
