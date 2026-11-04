"""Admin API startup validation (env root + enabled flag)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services.admin_passwords import MIN_ROOT_PASSWORD_LENGTH, root_password_too_short
from app.services.admin_root_crypto import clear_root_fingerprint_cache, warm_root_fingerprint_cache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.config import Settings

logger = logging.getLogger(__name__)


class AdminApiConfigError(ValueError):
    """Invalid ADMIN_* configuration."""


def validate_admin_env(settings: Settings) -> None:
    """Validate ADMIN_* env when the admin API is enabled.

    When ADMIN_API_ENABLED is false, root env vars are ignored (no startup failure).
    When enabled: root is optional, but login/password must be both set or both empty,
    and a set password must be at least MIN_ROOT_PASSWORD_LENGTH characters.
    """
    if settings.admin_api_enabled is not True:
        clear_root_fingerprint_cache()
        return

    raw_login = settings.admin_root_login
    raw_password = settings.admin_root_password
    login = raw_login.strip() if isinstance(raw_login, str) else ""
    password = raw_password if isinstance(raw_password, str) else ""
    has_login = bool(login)
    has_password = bool(password)

    if has_login ^ has_password:
        raise AdminApiConfigError("ADMIN_ROOT_LOGIN and ADMIN_ROOT_PASSWORD must both be set, or both empty")
    if has_password and root_password_too_short(password):
        raise AdminApiConfigError(f"ADMIN_ROOT_PASSWORD must be at least {MIN_ROOT_PASSWORD_LENGTH} characters")

    if has_login and has_password:
        warm_root_fingerprint_cache(login, password)
    else:
        clear_root_fingerprint_cache()


def log_admin_root_status(settings: Settings) -> None:
    if settings.admin_api_enabled is not True:
        return
    login = (settings.admin_root_login or "").strip()
    if login:
        logger.info("Admin root login enabled (login=%s); root is not stored in the database", login)
    else:
        logger.info("Admin API enabled without env root; DB admin users only")


async def assert_root_login_not_in_db(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Fail startup if ADMIN_ROOT_LOGIN collides with an admin_users.login row."""
    login = (settings.admin_root_login or "").strip()
    if not login or settings.admin_api_enabled is not True:
        return
    from sqlalchemy import select

    from app.models import AdminUser

    async with session_factory() as session:
        existing = (
            await session.execute(select(AdminUser.id).where(AdminUser.login == login).limit(1))
        ).scalar_one_or_none()
    if existing is not None:
        raise AdminApiConfigError(f"ADMIN_ROOT_LOGIN collides with an existing admin_users.login ({login!r})")


async def assert_admin_bootstrap_possible(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Without root env, fail startup unless at least one active DB admin exists."""
    if settings.admin_api_enabled is not True or (settings.admin_root_login or "").strip():
        return
    from app.services.admin_user_ops import count_active_admins

    async with session_factory() as session:
        count = await count_active_admins(session)
    if count == 0:
        raise AdminApiConfigError(
            "ADMIN_API_ENABLED is true but no root credentials are set and admin_users has no active admin; "
            "set ADMIN_ROOT_LOGIN and ADMIN_ROOT_PASSWORD in .env to bootstrap"
        )
