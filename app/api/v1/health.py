from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/is-ready")
async def is_ready(request: Request) -> dict[str, str]:
    async with request.app.state.session_factory() as session:
        await session.execute(text("SELECT 1"))
    pong = await request.app.state.redis.ping()
    if not pong:
        raise RuntimeError("redis ping failed")
    return {"status": "ready"}
