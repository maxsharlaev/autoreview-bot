from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import ApiKeyDep
from app.metrics import ops_snapshot

router = APIRouter()


@router.get("/ops/status")
async def ops_status(request: Request, _: ApiKeyDep) -> dict:
    return await ops_snapshot(request.app.state.session_factory, request.app.state.redis)
