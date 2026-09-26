from fastapi import APIRouter

from app.api.v1 import health, metrics, ops, pull_request, reviews

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(metrics.router, tags=["observability"])
api_router.include_router(ops.router, prefix="/api/v1", tags=["observability"])
api_router.include_router(pull_request.router, prefix="/api/v1", tags=["webhooks"])
api_router.include_router(reviews.router, prefix="/api/v1", tags=["reviews"])
