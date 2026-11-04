"""Atomic admin_users updates (session_version bumps in SQL)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminUser

# Fixed pg_advisory_xact_lock key serializing "last active admin" checks.
ADMIN_GUARD_LOCK_KEY = 7_340_001


class LastActiveAdminError(Exception):
    """Change would leave no active admin while root is not configured."""


class AdminLoginConflictError(Exception):
    """admin_users.login unique constraint violated."""


async def create_admin_user(session: AsyncSession, user: AdminUser) -> AdminUser:
    """Insert and commit; a concurrent duplicate login raises AdminLoginConflictError."""
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AdminLoginConflictError(user.login) from exc
    await session.refresh(user)
    return user


async def count_active_admins(session: AsyncSession, *, exclude_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count()).select_from(AdminUser).where(AdminUser.role == "admin", AdminUser.is_active.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(AdminUser.id != exclude_id)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def ensure_not_last_active_admin(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """Take the advisory xact lock, then refuse if user_id is the only active admin.

    The lock is held until the caller's commit/rollback, so check + update are serialized.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMIN_GUARD_LOCK_KEY})
    row = (
        await session.execute(select(AdminUser.role, AdminUser.is_active).where(AdminUser.id == user_id))
    ).one_or_none()
    if row is None or row[0] != "admin" or not row[1]:
        return
    if await count_active_admins(session, exclude_id=user_id) == 0:
        raise LastActiveAdminError(str(user_id))


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
    protect_last_admin: bool = False,
) -> AdminUser | None:
    """Apply optional role/is_active; bump session_version in SQL when requested.

    With protect_last_admin, a demotion/deactivation is checked under the advisory lock first.
    """
    if protect_last_admin and (role == "viewer" or is_active is False):
        await ensure_not_last_active_admin(session, user_id=user_id)
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
