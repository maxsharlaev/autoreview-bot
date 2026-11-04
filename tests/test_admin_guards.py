"""Admin guards: rootless bootstrap, last active admin, duplicate login (unit, no DB)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.admin_config import AdminApiConfigError, assert_admin_bootstrap_possible
from app.services.admin_user_ops import (
    AdminLoginConflictError,
    LastActiveAdminError,
    create_admin_user,
    ensure_not_last_active_admin,
)
from sqlalchemy.exc import IntegrityError


class _Result:
    def __init__(self, scalar=None, row=None):  # noqa: ANN001
        self._scalar = scalar
        self._row = row

    def scalar_one(self):  # noqa: ANN201
        return self._scalar

    def one_or_none(self):  # noqa: ANN201
        return self._row


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self.results = list(results)
        self.sql: list[str] = []
        self.rollback = AsyncMock()

    async def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN201
        self.sql.append(str(stmt))
        return self.results.pop(0) if self.results else _Result()

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


def _settings(root: str = "") -> SimpleNamespace:
    return SimpleNamespace(admin_api_enabled=True, admin_root_login=root)


@pytest.mark.asyncio
async def test_rootless_startup_fails_without_active_admin() -> None:
    session = _Session([_Result(scalar=0)])
    with pytest.raises(AdminApiConfigError, match="ADMIN_ROOT_LOGIN and ADMIN_ROOT_PASSWORD"):
        await assert_admin_bootstrap_possible(lambda: session, _settings())


@pytest.mark.asyncio
async def test_rootless_startup_ok_with_active_admin_and_skipped_with_root() -> None:
    await assert_admin_bootstrap_possible(lambda: _Session([_Result(scalar=1)]), _settings())
    factory = MagicMock()
    await assert_admin_bootstrap_possible(factory, _settings(root="rootadmin"))
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_last_active_admin_guard_takes_lock_and_refuses() -> None:
    session = _Session([_Result(), _Result(row=("admin", True)), _Result(scalar=0)])
    with pytest.raises(LastActiveAdminError):
        await ensure_not_last_active_admin(session, user_id=uuid.uuid4())
    assert "pg_advisory_xact_lock" in session.sql[0]


@pytest.mark.asyncio
async def test_last_active_admin_guard_allows_when_other_admin_exists() -> None:
    session = _Session([_Result(), _Result(row=("admin", True)), _Result(scalar=1)])
    await ensure_not_last_active_admin(session, user_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_create_admin_user_maps_integrity_error() -> None:
    session = MagicMock()
    session.commit = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("uq_admin_users_login")))
    session.rollback = AsyncMock()
    with pytest.raises(AdminLoginConflictError):
        await create_admin_user(session, SimpleNamespace(login="dup"))
    session.rollback.assert_awaited_once()
