"""DB tests: last-admin guard and duplicate-login create under real concurrency (Postgres)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import uuid
from pathlib import Path

import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


def _skip_reason() -> str | None:
    if not TEST_DB_URL:
        return "TEST_DATABASE_URL not set"
    match = re.search(r"/([^/?]+)(?:\?|$)", TEST_DB_URL)
    if not match or not match.group(1).endswith("_test"):
        return "Database name must end with '_test'"
    return None


_skip = _skip_reason()
pytestmark = [pytest.mark.skipif(bool(_skip), reason=_skip or ""), pytest.mark.asyncio, pytest.mark.postgres]


def _asyncpg_url() -> str:
    url = TEST_DB_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", _asyncpg_url())
    cfg.attributes["use_test_url"] = True
    cfg.attributes["configure_logger"] = False
    # env.py calls asyncio.run(); keep it off the test loop.
    with concurrent.futures.ThreadPoolExecutor() as ex:
        ex.submit(command.upgrade, cfg, "head").result()


@pytest.fixture
async def factory():  # noqa: ANN201
    from app.models import AdminUser
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    _upgrade_head()
    engine = create_async_engine(_asyncpg_url())
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        await s.execute(delete(AdminUser))
        await s.commit()
    yield sf
    async with sf() as s:
        await s.execute(delete(AdminUser))
        await s.commit()
    await engine.dispose()


def _user(login: str, role: str = "admin"):  # noqa: ANN202
    from app.models import AdminUser

    return AdminUser(id=uuid.uuid4(), login=login, password_hash="x", role=role, is_active=True, session_version=0)


async def test_concurrent_demotions_keep_one_active_admin(factory) -> None:  # noqa: ANN001
    from app.services.admin_user_ops import LastActiveAdminError, count_active_admins, update_user_atomic

    a, b = _user("admin-a"), _user("admin-b")
    async with factory() as s:
        s.add_all([a, b])
        await s.commit()

    async def demote(user_id: uuid.UUID, how: str) -> str:
        async with factory() as s:
            kwargs = {"role": "viewer"} if how == "role" else {"is_active": False}
            try:
                await update_user_atomic(s, user_id=user_id, bump_session=True, protect_last_admin=True, **kwargs)
                await asyncio.sleep(0.05)  # hold the lock so the other request really waits
                await s.commit()
                return "ok"
            except LastActiveAdminError:
                await s.rollback()
                return "conflict"

    results = await asyncio.gather(demote(a.id, "role"), demote(b.id, "active"))
    assert sorted(results) == ["conflict", "ok"]
    async with factory() as s:
        assert await count_active_admins(s) == 1


async def test_concurrent_create_same_login_one_conflict(factory) -> None:  # noqa: ANN001
    from app.services.admin_user_ops import AdminLoginConflictError, create_admin_user

    async def create() -> str:
        async with factory() as s:
            try:
                await create_admin_user(s, _user("dup-login", role="viewer"))
                return "ok"
            except AdminLoginConflictError:
                return "conflict"

    assert sorted(await asyncio.gather(create(), create())) == ["conflict", "ok"]


async def test_rootless_bootstrap_check_against_db(factory) -> None:  # noqa: ANN001
    from types import SimpleNamespace

    from app.services.admin_config import AdminApiConfigError, assert_admin_bootstrap_possible

    settings = SimpleNamespace(admin_api_enabled=True, admin_root_login="")
    with pytest.raises(AdminApiConfigError):
        await assert_admin_bootstrap_possible(factory, settings)
    async with factory() as s:
        s.add(_user("boot-admin"))
        await s.commit()
    await assert_admin_bootstrap_possible(factory, settings)
