"""Admin session authentication dependencies."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Cookie, Depends, HTTPException, Request

from app.config import get_settings
from app.services.admin_root_crypto import get_root_fingerprint
from app.services.admin_sessions import destroy_session, load_session

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
    name = settings.admin_session_cookie_name
    if name != "opr_admin_session":
        return request.cookies.get(name)
    return opr_admin_session


def _hmac_eq(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


async def require_admin_principal(
    request: Request,
    token: Annotated[str | None, Depends(get_admin_session_token)],
) -> AdminPrincipal:
    settings = get_settings()
    if settings.admin_api_enabled is not True:
        raise HTTPException(status_code=404, detail="not found")
    redis = request.app.state.redis
    session = await load_session(redis, token)
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")

    if session.kind == "root":
        root_login = settings.admin_root_login if isinstance(settings.admin_root_login, str) else ""
        root_password = settings.admin_root_password if isinstance(settings.admin_root_password, str) else ""
        root_login = root_login.strip()
        if not root_login or not root_password:
            await destroy_session(redis, token)
            raise HTTPException(status_code=401, detail="not authenticated")
        expected = get_root_fingerprint(root_login, root_password)
        presented = session.root_fingerprint or ""
        if not _hmac_eq(expected, presented) or not _hmac_eq(session.login, root_login):
            await destroy_session(redis, token)
            raise HTTPException(status_code=401, detail="not authenticated")
        return AdminPrincipal(
            kind="root",
            role="admin",
            login=root_login,
            user_id=None,
            session_token=session.token,
        )

    if not session.user_id:
        await destroy_session(redis, token)
        raise HTTPException(status_code=401, detail="not authenticated")

    from sqlalchemy import select

    from app.models import AdminUser

    factory = request.app.state.session_factory
    async with factory() as db:
        user = (
            await db.execute(select(AdminUser).where(AdminUser.id == uuid.UUID(session.user_id)))
        ).scalar_one_or_none()
    if user is None or not user.is_active:
        await destroy_session(redis, token)
        raise HTTPException(status_code=401, detail="not authenticated")
    if session.session_version is None or session.session_version != user.session_version:
        await destroy_session(redis, token)
        raise HTTPException(status_code=401, detail="not authenticated")
    role: AdminRole = user.role if user.role in ("admin", "viewer") else "viewer"
    return AdminPrincipal(
        kind="user",
        role=role,
        login=user.login,
        user_id=str(user.id),
        session_token=session.token,
    )


def require_admin_role(principal: AdminPrincipal) -> AdminPrincipal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


AdminPrincipalDep = Annotated[AdminPrincipal, Depends(require_admin_principal)]
