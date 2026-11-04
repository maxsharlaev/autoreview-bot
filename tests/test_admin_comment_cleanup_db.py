"""Postgres: manual sensitive-comment cleanup (all runs sharing the comment) and migration 011."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, inspect, select
from tests.test_admin_api import FakeGitHub, FakeRedis, _app, _settings
from tests.test_admin_findings_db import ROOT_LOGIN, ROOT_PASSWORD, _alembic
from tests.test_admin_guards_db import _asyncpg_url, _skip_reason, _upgrade_head

_skip = _skip_reason()
pytestmark = [pytest.mark.skipif(bool(_skip), reason=_skip or ""), pytest.mark.postgres]

REPO = "acme/cleanup-api"
SHARED_URL = "https://github.com/acme/cleanup-api/pull/9#issuecomment-555"
OTHER_URL = "https://github.com/acme/cleanup-api/pull/9#issuecomment-777"


@pytest.fixture
async def factory():  # noqa: ANN201
    from app.models import Repository
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    _upgrade_head()
    engine = create_async_engine(_asyncpg_url())
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with sf() as s:
            await s.execute(delete(Repository).where(Repository.full_name == REPO))
            await s.commit()

    await _clean()
    yield sf
    await _clean()
    await engine.dispose()


async def _seed(sf) -> list[uuid.UUID]:  # noqa: ANN001
    from app.models import PullRequest, Repository, ReviewRun

    async with sf() as s:
        repo = Repository(id=uuid.uuid4(), full_name=REPO)
        pr = PullRequest(id=uuid.uuid4(), repository_id=repo.id, number=9)
        runs = [
            ReviewRun(
                id=uuid.uuid4(),
                pull_request_id=pr.id,
                base_sha="a" * 40,
                head_sha=c * 40,
                comment_url=url,
                has_sensitive_content=True,
            )
            for c, url in (("b", SHARED_URL), ("c", SHARED_URL), ("d", OTHER_URL))
        ]
        s.add(repo)
        await s.flush()
        s.add(pr)
        await s.flush()
        s.add_all(runs)
        await s.commit()
        return [r.id for r in runs]


async def test_cleanup_flow_marks_all_runs_sharing_the_comment(factory) -> None:  # noqa: ANN001
    from app.api.deps import get_session
    from app.models import ReviewRun
    from app.services.admin_root_crypto import clear_root_credentials, load_root_credentials

    ids = await _seed(factory)
    app = _app(None, FakeRedis())  # type: ignore[arg-type]

    async def real_session() -> AsyncIterator:
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = real_session
    app.state.session_factory = factory
    settings = _settings()
    load_root_credentials(ROOT_LOGIN, ROOT_PASSWORD)
    failing, ok = FakeGitHub(error_status=403), FakeGitHub()
    url = f"/api/v1/admin/runs/{ids[0]}/cleanup-comment"

    async def audit() -> list:
        async with factory() as s:
            rows = (await s.execute(select(ReviewRun).where(ReviewRun.id.in_(ids)))).scalars().all()
            return sorted((r.head_sha[0], r.comment_cleaned_by, r.comment_cleanup_reason) for r in rows)

    try:
        with (
            patch("app.api.admin_auth.get_settings", return_value=settings),
            patch("app.api.v1.admin.get_settings", return_value=settings),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                login = {"login": ROOT_LOGIN, "password": ROOT_PASSWORD}
                assert (await client.post("/api/v1/admin/login", json=login)).status_code == 200

                with patch("app.api.v1.admin._github_for_owner", return_value=failing):
                    assert (await client.post(url, json={"reason": "leak"})).status_code == 502
                assert all(by is None for _, by, _ in await audit())

                with patch("app.api.v1.admin._github_for_owner", return_value=ok):
                    r = await client.post(url, json={"reason": " leak "})
                    assert r.status_code == 200 and r.json()["comment_cleaned_by"] == ROOT_LOGIN
                    assert (await client.post(url, json={})).status_code == 200  # repeat: no GitHub call
                assert ok.calls == [("acme", "cleanup-api", 555)]
                assert await audit() == [("b", ROOT_LOGIN, "leak"), ("c", ROOT_LOGIN, "leak"), ("d", None, None)]

                params = {"repo": REPO, "sensitive": "true", "cleaned": "false"}
                pending = (await client.get("/api/v1/admin/runs", params=params)).json()
                assert [item["id"] for item in pending["items"]] == [str(ids[2])]
    finally:
        clear_root_credentials()


async def test_migration_011_up_down(factory) -> None:  # noqa: ANN001
    from sqlalchemy.ext.asyncio import create_async_engine

    cols = {"comment_cleaned_by", "comment_cleaned_at", "comment_cleanup_reason"}

    async def run_columns() -> set[str]:
        engine = create_async_engine(_asyncpg_url())
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("review_runs")})
        await engine.dispose()
        return names

    assert cols <= await run_columns()
    _alembic("010_finding_admin_status", down=True)
    assert not cols & await run_columns()
    _alembic("head")
    assert cols <= await run_columns()
