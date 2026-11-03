"""DB test: concurrent password resets bump session_version atomically."""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


def _validate_test_database_url() -> str | None:
    if not TEST_DB_URL:
        return "TEST_DATABASE_URL not set"
    match = re.search(r"/([^/?]+)(?:\?|$)", TEST_DB_URL)
    if not match:
        return "Could not parse database name from TEST_DATABASE_URL"
    if not match.group(1).endswith("_test"):
        return f"Database name must end with '_test' (got: {match.group(1)})"
    return None


_skip = _validate_test_database_url()
requires_test_postgres = pytest.mark.skipif(bool(_skip), reason=_skip or "")


def _asyncpg_url() -> str:
    url = TEST_DB_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@requires_test_postgres
@pytest.mark.asyncio
async def test_concurrent_password_resets_bump_version_twice() -> None:
    from app.models import AdminUser
    from app.services.admin_passwords import hash_password
    from app.services.admin_sessions import create_session, load_session
    from app.services.admin_user_ops import reset_password_atomic
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_asyncpg_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()

    async with factory() as session:
        session.add(
            AdminUser(
                id=user_id,
                login=f"concurrent-{user_id.hex[:8]}",
                password_hash=hash_password("initial-password1"),
                role="admin",
                is_active=True,
                session_version=0,
            )
        )
        await session.commit()

    async def one_reset(password: str) -> int:
        async with factory() as session:
            updated = await reset_password_atomic(session, user_id=user_id, password_hash=hash_password(password))
            await session.commit()
            assert updated is not None
            return int(updated.session_version)

    v1, v2 = await asyncio.gather(one_reset("reset-password-aa"), one_reset("reset-password-bb"))
    assert sorted([v1, v2]) == [1, 2]

    async with factory() as session:
        from sqlalchemy import select

        row = (await session.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one()
        assert row.session_version == 2
        final_version = row.session_version

    # Session minted at version after "first" logical reset (1) must be rejected vs DB (2)
    class _MemRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        async def set(self, key: str, value: str, ex: int | None = None) -> bool:
            self.store[key] = value
            return True

        async def get(self, key: str) -> str | None:
            return self.store.get(key)

        async def delete(self, *keys: str) -> int:
            n = 0
            for k in keys:
                if k in self.store:
                    del self.store[k]
                    n += 1
            return n

    redis = _MemRedis()
    sess = await create_session(
        redis,  # type: ignore[arg-type]
        kind="user",
        role="admin",
        login="x",
        user_id=str(user_id),
        ttl_seconds=60,
        session_version=1,
    )
    loaded = await load_session(redis, sess.token)  # type: ignore[arg-type]
    assert loaded is not None
    assert loaded.session_version == 1
    assert loaded.session_version != final_version

    async with factory() as session:
        from sqlalchemy import delete

        await session.execute(delete(AdminUser).where(AdminUser.id == user_id))
        await session.commit()
    await engine.dispose()
