"""Admin session authentication dependencies."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Cookie, Depends, HTTPException, Request

from app.config import get_settings
from app.services.admin_sessions import load_session

AdminRole = Literal["admin", "viewer"]


@dataclass(frozen=True)
class AdminPrincipal:
    kind: Literal["root", "user"]
    role: AdminRole
    login: str
    user_id: str | None
    session_token: str


async def get_admin_session_token(
    request: Request,
    opr_admin_session: str | None = Cookie(default=None, alias="opr_admin_session"),
) -> str | None:
    settings = get_settings()
    # Allow tests / alternate cookie name via settings
    name = settings.admin_session_cookie_name
    if name != "opr_admin_session":
        return request.cookies.get(name)
    return opr_admin_session


async def require_admin_principal(
    request: Request,
    token: Annotated[str | None, Depends(get_admin_session_token)],
) -> AdminPrincipal:
    settings = get_settings()
    if not settings.admin_api_enabled:
        raise HTTPException(status_code=404, detail="not found")
    redis = request.app.state.redis
    session = await load_session(redis, token)
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")

    if session.kind == "user":
        # Re-check DB: deactivated users must not keep working sessions.
        if not session.user_id:
            raise HTTPException(status_code=401, detail="not authenticated")
        from sqlalchemy import select

        from app.models import AdminUser

        factory = request.app.state.session_factory
        async with factory() as db:
            user = (
                await db.execute(select(AdminUser).where(AdminUser.id == uuid.UUID(session.user_id)))
            ).scalar_one_or_none()
        if user is None or not user.is_active:
            from app.services.admin_sessions import destroy_session

            await destroy_session(redis, token)
            raise HTTPException(status_code=401, detail="not authenticated")
        # Role may have been updated; prefer DB role for authorization.
        role: AdminRole = user.role if user.role in ("admin", "viewer") else session.role
        return AdminPrincipal(
            kind="user",
            role=role,  # type: ignore[arg-type]
            login=user.login,
            user_id=str(user.id),
            session_token=session.token,
        )

    return AdminPrincipal(
        kind="root",
        role="admin",
        login=session.login,
        user_id=None,
        session_token=session.token,
    )


def require_admin_role(principal: AdminPrincipal) -> AdminPrincipal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


AdminPrincipalDep = Annotated[AdminPrincipal, Depends(require_admin_principal)]
