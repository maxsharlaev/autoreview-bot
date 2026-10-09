from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.owners.registry import get_owner_registry
from app.security.access import extract_presented_key, verify_api_key


@dataclass
class Principal:
    """Authenticated principal for API requests."""

    kind: str  # "operator" (global key) or "owner" (scoped key)
    owner_id: str | None  # Set for owner-scoped keys


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
    """Legacy API key check for backward compatibility."""
    settings = get_settings()
    presented = extract_presented_key(authorization, x_api_key)
    if not verify_api_key(settings.review_api_key, presented):
        raise HTTPException(status_code=401, detail="invalid access key")


async def require_principal(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> Principal:
    """Multi-owner principal authentication.

    Returns:
        Principal with kind='operator' for global REVIEW_API_KEY
        Principal with kind='owner' and owner_id set for owner-scoped keys

    Raises:
        HTTPException(401) if key is invalid
    """
    settings = get_settings()
    registry = get_owner_registry()
    presented = extract_presented_key(authorization, x_api_key)

    if not presented:
        raise HTTPException(status_code=401, detail="invalid access key")

    kind, owner_id = registry.principal_for_key(presented, settings.review_api_key)
    if not kind:
        raise HTTPException(status_code=401, detail="invalid access key")

    return Principal(kind=kind, owner_id=owner_id)


def resolve_owner_selector(
    x_review_owner: str | None = Header(default=None, alias="X-Review-Owner"),
    owner: str | None = Query(default=None),
) -> str | None:
    """Resolve owner from X-Review-Owner header or ?owner= query param.

    Raises:
        HTTPException(400) if both are provided and different
    """
    if x_review_owner and owner:
        if x_review_owner.strip().lower() != owner.strip().lower():
            raise HTTPException(
                status_code=400,
                detail="X-Review-Owner header and owner query parameter are both set and different",
            )
    return x_review_owner or owner


ApiKeyDep = Annotated[None, Depends(require_api_key)]
PrincipalDep = Annotated[Principal, Depends(require_principal)]
OwnerSelectorDep = Annotated[str | None, Depends(resolve_owner_selector)]
