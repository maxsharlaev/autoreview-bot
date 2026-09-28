from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.security.access import extract_presented_key, verify_api_key


async def get_session(request: Request) -> AsyncSession:
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_redis(request: Request):
    return request.app.state.redis


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> None:
    settings = get_settings()
    presented = extract_presented_key(authorization, x_api_key)
    if not verify_api_key(settings.review_api_key, presented):
        raise HTTPException(status_code=401, detail="invalid access key")


ApiKeyDep = Annotated[None, Depends(require_api_key)]


async def require_result_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> None:
    settings = get_settings()
    if not settings.result_api_key or settings.result_api_key == settings.review_api_key:
        raise HTTPException(status_code=503, detail="private result access is disabled")
    presented = extract_presented_key(authorization, x_api_key)
    if not verify_api_key(settings.result_api_key, presented):
        raise HTTPException(status_code=401, detail="invalid result access key")


ResultKeyDep = Annotated[None, Depends(require_result_api_key)]
