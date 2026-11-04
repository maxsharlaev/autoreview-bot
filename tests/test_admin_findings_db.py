"""Postgres: admin findings API + orchestrator store (suppress, re-report, unsuppress) and migration 010."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, inspect, select
from tests.test_admin_api import FakeRedis
from tests.test_admin_guards_db import _asyncpg_url, _skip_reason, _upgrade_head
from tests.test_finding_suppression import _view

_skip = _skip_reason()
pytestmark = [pytest.mark.skipif(bool(_skip), reason=_skip or ""), pytest.mark.postgres]

ROOT_LOGIN = "rootadmin"
ROOT_PASSWORD = "root-password-16c"
VIEWER_PASSWORD = "viewer-pass-12"


def _alembic(target: str, *, down: bool = False) -> None:
    import concurrent.futures
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", _asyncpg_url())
    cfg.attributes["use_test_url"] = True
    cfg.attributes["configure_logger"] = False
    fn = command.downgrade if down else command.upgrade
    with concurrent.futures.ThreadPoolExecutor() as ex:
        ex.submit(fn, cfg, target).result()


@pytest.fixture
async def factory():  # noqa: ANN201
    from app.models import AdminUser, Repository
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    _upgrade_head()
    engine = create_async_engine(_asyncpg_url())
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with sf() as s:
            await s.execute(delete(Repository).where(Repository.full_name == "acme/findings-api"))
            await s.execute(delete(AdminUser).where(AdminUser.login == "vera"))
            await s.commit()

    await _clean()
    yield sf
    await _clean()
    await engine.dispose()


async def _seed(sf):  # noqa: ANN001, ANN202
    from app.models import Finding, PullRequest, Repository, ReviewRun

    async with sf() as s:
        repo = Repository(id=uuid.uuid4(), full_name="acme/findings-api")
        pr = PullRequest(id=uuid.uuid4(), repository_id=repo.id, number=7)
        runs = [ReviewRun(id=uuid.uuid4(), pull_request_id=pr.id, base_sha="a" * 40, head_sha=c * 40) for c in "bcd"]
        finding = Finding(
            id=uuid.uuid4(),
            pull_request_id=pr.id,
            first_seen_run_id=runs[0].id,
            last_seen_run_id=runs[0].id,
            owner_id="default",
            stable_id="sup-1",
            severity="P2",
            category="correctness",
            path="app/x.py",
            line=10,
            title="Issue",
            scenario="scenario",
            evidence="evidence",
            recommendation="fix it",
            current_status="open",
        )
        s.add(repo)
        await s.flush()
        s.add(pr)
        await s.flush()
        s.add_all(runs)
        await s.flush()
        s.add(finding)
        await s.commit()
        return pr.id, [r.id for r in runs], finding.id


async def _store_again(sf, pr_id: uuid.UUID, run_id: uuid.UUID) -> None:  # noqa: ANN001
    from app.models import PullRequest
    from app.services.orchestrator import _store_findings
    from app.services.publisher import empty_verified
    from sqlalchemy.orm import selectinload

    async with sf() as s:
        pr = (
            await s.execute(
                select(PullRequest).options(selectinload(PullRequest.findings)).where(PullRequest.id == pr_id)
            )
        ).scalar_one()
        verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
        verified.findings = [_view()]
        run = type("Run", (), {"id": run_id})()
        await _store_findings(
            s,
            pr,
            run,
            verified,
            fresh_ids={"sup-1"},
            details_visibility="private",
            owner_id="default",
            owner_findings=list(pr.findings),
        )
        await s.commit()


async def test_suppress_reappear_unsuppress_flow(factory) -> None:  # noqa: ANN001
    import httpx
    from app.api.deps import get_session
    from app.models import AdminUser, Finding
    from app.services.admin_passwords import hash_password
    from app.services.admin_root_crypto import clear_root_credentials, load_root_credentials
    from tests.test_admin_api import _app, _settings

    pr_id, run_ids, fid = await _seed(factory)
    async with factory() as s:
        s.add(AdminUser(id=uuid.uuid4(), login="vera", password_hash=hash_password(VIEWER_PASSWORD), role="viewer"))
        await s.commit()

    app = _app(None, FakeRedis())  # type: ignore[arg-type]

    async def real_session() -> AsyncIterator:
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = real_session
    app.state.session_factory = factory
    settings = _settings()
    load_root_credentials(ROOT_LOGIN, ROOT_PASSWORD)
    from unittest.mock import patch

    try:
        with (
            patch("app.api.admin_auth.get_settings", return_value=settings),
            patch("app.api.v1.admin.get_settings", return_value=settings),
        ):
            transport = httpx.ASGITransport(app=app)
            async with (
                httpx.AsyncClient(transport=transport, base_url="http://test") as viewer,
                httpx.AsyncClient(transport=transport, base_url="http://test") as root,
            ):
                assert (
                    await viewer.post("/api/v1/admin/login", json={"login": "vera", "password": VIEWER_PASSWORD})
                ).status_code == 200
                assert (
                    await root.post("/api/v1/admin/login", json={"login": ROOT_LOGIN, "password": ROOT_PASSWORD})
                ).status_code == 200

                listed = (
                    await viewer.get("/api/v1/admin/findings", params={"repo": "acme/findings-api", "pr": 7})
                ).json()
                assert listed["count"] == 1 and listed["items"][0]["id"] == str(fid)
                assert "evidence" not in listed["items"][0]
                assert (await viewer.get(f"/api/v1/admin/findings/{fid}")).json()["evidence"] == "evidence"
                assert (
                    await viewer.post(f"/api/v1/admin/findings/{fid}/suppress", json={"status": "accepted"})
                ).status_code == 403

                r = await root.post(
                    f"/api/v1/admin/findings/{fid}/suppress", json={"status": "false_positive", "reason": " noise "}
                )
                assert r.status_code == 200
                body = r.json()
                assert body["current_status"] == "false_positive"
                assert body["status_changed_by"] == ROOT_LOGIN and body["status_reason"] == "noise"
                assert body["status_changed_at"]
                filtered = (
                    await viewer.get(
                        "/api/v1/admin/findings", params=[("status", "accepted"), ("status", "false_positive")]
                    )
                ).json()
                assert [i["id"] for i in filtered["items"] if i["id"] == str(fid)] == [str(fid)]

                # Same finding reported again by a new run: stays suppressed, only last_seen moves.
                await _store_again(factory, pr_id, run_ids[1])
                async with factory() as s:
                    row = (await s.execute(select(Finding).where(Finding.id == fid))).scalar_one()
                    assert row.current_status == "false_positive"
                    assert row.last_seen_run_id == run_ids[1]
                    assert row.status_reason == "noise"

                r = await root.post(f"/api/v1/admin/findings/{fid}/unsuppress", json={})
                assert r.status_code == 200
                assert r.json()["current_status"] == "open" and r.json()["status_reason"] is None
                assert (await root.post(f"/api/v1/admin/findings/{fid}/unsuppress", json={})).status_code == 409

                await _store_again(factory, pr_id, run_ids[2])
                async with factory() as s:
                    row = (await s.execute(select(Finding).where(Finding.id == fid))).scalar_one()
                    assert row.current_status == "open"
                    assert row.last_seen_run_id == run_ids[2]
    finally:
        clear_root_credentials()


async def test_migration_010_up_down(factory) -> None:  # noqa: ANN001
    from sqlalchemy.ext.asyncio import create_async_engine

    cols = {"status_changed_by", "status_changed_at", "status_reason"}

    async def finding_columns() -> set[str]:
        engine = create_async_engine(_asyncpg_url())
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("findings")})
        await engine.dispose()
        return names

    assert cols <= await finding_columns()
    _alembic("009_admin_users", down=True)
    assert not cols & await finding_columns()
    _alembic("head")
    assert cols <= await finding_columns()
