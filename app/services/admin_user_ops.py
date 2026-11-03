"""Atomic admin_users updates (session_version bumps in SQL)."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminUser


async def reset_password_atomic(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    password_hash: str,
) -> AdminUser | None:
    """Set password_hash and bump session_version in one UPDATE ... RETURNING."""
    result = await session.execute(
        update(AdminUser)
        .where(AdminUser.id == user_id)
        .values(
            password_hash=password_hash,
            session_version=AdminUser.session_version + 1,
        )
        .returning(AdminUser)
    )
    return result.scalar_one_or_none()


async def update_user_atomic(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: str | None = None,
    is_active: bool | None = None,
    bump_session: bool = False,
) -> AdminUser | None:
    """Apply optional role/is_active; bump session_version in SQL when requested."""
    values: dict[str, object] = {}
    if role is not None:
        values["role"] = role
    if is_active is not None:
        values["is_active"] = is_active
    if bump_session:
        values["session_version"] = AdminUser.session_version + 1
    if not values:
        return (await session.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()
    result = await session.execute(
        update(AdminUser).where(AdminUser.id == user_id).values(**values).returning(AdminUser)
    )
    return result.scalar_one_or_none()
