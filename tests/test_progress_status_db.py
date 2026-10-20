"""Progress status and the sensitive-content flag against Postgres (migration 008, issue #7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.test_cross_owner_db import (
    _clear_findings,
    _downgrade_migrations,
    _get_alembic_config,
    _get_asyncpg_url,
    _run_migrations,
    requires_test_postgres,
)
from tests.test_progress_status import COMMENT_URL, HEAD, FakeGitHub, Harness, _finding, _verified

pytestmark = requires_test_postgres

NEW_HEAD = "e" * 40


@pytest.fixture
async def session_factory():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    config = _get_alembic_config()
    await _clear_findings()
    _downgrade_migrations(config, "base")
    _run_migrations(config, "head")
    engine = create_async_engine(_get_asyncpg_url())
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()
    await _clear_findings()
    _downgrade_migrations(config, "base")


async def _columns(conn) -> dict[str, str]:
    from sqlalchemy import text

    rows = await conn.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'review_runs' AND column_name IN "
            "('progress_status', 'comment_url', 'has_sensitive_content')"
        )
    )
    return {name: nullable for name, nullable in rows}


@pytest.mark.asyncio
async def test_migration_008_adds_nullable_columns_and_downgrades_cleanly() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    config = _get_alembic_config()
    await _clear_findings()
    _downgrade_migrations(config, "base")
    _run_migrations(config, "007_findings_owner_id")
    engine = create_async_engine(_get_asyncpg_url())
    repo_id, pr_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            assert await _columns(conn) == {}
            await conn.execute(
                text(
                    "INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id) "
                    "VALUES (:id, 'org/old', true, 'default', '[]', 'default')"
                ),
                {"id": repo_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO pull_requests (id, repository_id, number, html_url, title, author, state, base_sha, "
                    "head_sha, head_ref, is_draft, is_fork) VALUES (:id, :repo, 1, '', '', '', 'open', 'b', 'h', "
                    "'main', false, false)"
                ),
                {"id": pr_id, "repo": repo_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO review_runs (id, pull_request_id, trigger, base_sha, head_sha, status, "
                    "prompt_version, policy_version, owner_id) VALUES (:id, :pr, 'webhook', 'b', 'h', "
                    "'completed', 'v1', 'v1', 'default')"
                ),
                {"id": run_id, "pr": pr_id},
            )
        _run_migrations(config, "008_review_run_progress")
        async with engine.begin() as conn:
            assert await _columns(conn) == {
                "progress_status": "YES",
                "comment_url": "YES",
                "has_sensitive_content": "YES",
            }
            row = (
                await conn.execute(
                    text("SELECT has_sensitive_content, comment_url, progress_status FROM review_runs WHERE id = :id"),
                    {"id": run_id},
                )
            ).one()
            assert tuple(row) == (None, None, None)  # pre-existing rows stay unknown
            await conn.execute(
                text('UPDATE review_runs SET has_sensitive_content = true, progress_status = \'{"state": "queued"}\'')
            )
        _downgrade_migrations(config, "007_findings_owner_id")
        async with engine.begin() as conn:
            assert await _columns(conn) == {}
            count = (await conn.execute(text("SELECT count(*) FROM review_runs"))).scalar_one()
            assert count == 1
        _run_migrations(config, "head")
    finally:
        await engine.dispose()
        await _clear_findings()
        _downgrade_migrations(config, "base")


async def _queue(session_factory, reporter, head_sha: str):
    from app.services.review_enqueue import queue_review

    redis = MagicMock()
    redis.enqueue_job = AsyncMock(return_value=MagicMock(job_id=f"job-{head_sha[:4]}"))
    async with session_factory() as session:
        return await queue_review(
            session,
            redis,
            full_name="org/repo",
            number=7,
            pr_payload={"number": 7, "title": "ABC-12 validate", "head": {"ref": "feature/ABC-12"}},
            head_sha=head_sha,
            base_sha="b" * 40,
            is_fork=False,
            owner_id="org-a",
            progress=reporter,
        )


@pytest.mark.asyncio
async def test_queue_stores_check_run_and_new_push_cancels_previous_sha(session_factory) -> None:
    from app.config import AppConfig
    from app.models import ReviewRun
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    first = await _queue(session_factory, reporter, HEAD)
    async with session_factory() as session:
        stored = await session.get(ReviewRun, first.id)
        assert stored.progress_status["check_run_id"] == 101
        assert stored.progress_status["state"] == "queued"
        assert stored.has_sensitive_content is None

    second = await _queue(session_factory, reporter, NEW_HEAD)
    async with session_factory() as session:
        old = await session.get(ReviewRun, first.id)
        new = await session.get(ReviewRun, second.id)
        assert old.status == "cancelled"
        assert old.progress_status["state"] == "completed"
        assert old.progress_status["conclusion"] == "cancelled"
        assert new.progress_status["state"] == "queued"
    cancel = [call for call in fake.calls if call[0] == "update"]
    assert len(cancel) == 1
    assert cancel[0][1] == 101 and cancel[0][2]["conclusion"] == "cancelled"
    assert [call[1]["head_sha"] for call in fake.calls if call[0] == "create"] == [HEAD, NEW_HEAD]


@pytest.mark.asyncio
async def test_review_persists_sensitive_flag_comment_url_and_final_status(
    session_factory, monkeypatch, tmp_path
) -> None:
    from app.config import AppConfig
    from app.models import ReviewRun
    from app.services.orchestrator import run_review
    from app.services.progress_status import ProgressReporter

    h = Harness(monkeypatch, tmp_path, verified=_verified([_finding("P1", security=True)]))
    queued = await _queue(session_factory, ProgressReporter(h.github, "check_run", AppConfig()), HEAD)
    async with session_factory() as session:
        result = await run_review(
            session,
            queued.id,
            settings=SimpleNamespace(openai_api_key=""),
            github=h.github,
            jira=h.jira,
            publisher=h.publisher,
            codex_fn=h.codex,
            registry=h.registry,
        )
        assert result.status == "completed"
    async with session_factory() as session:
        stored = await session.get(ReviewRun, queued.id)
        assert stored.has_sensitive_content is True
        assert stored.comment_url == COMMENT_URL
        assert stored.progress_status["state"] == "completed"
        assert stored.progress_status["conclusion"] == "neutral"
        assert stored.progress_status["check_run_id"] == 101
    assert h.github.check_states() == ["queued", "in_progress", "completed"]


@pytest.mark.asyncio
async def test_crashed_review_stores_failed_status(session_factory, monkeypatch, tmp_path) -> None:
    from app.config import AppConfig
    from app.models import ReviewRun
    from app.services.orchestrator import run_review
    from app.services.progress_status import ProgressReporter

    h = Harness(monkeypatch, tmp_path, codex_error=RuntimeError("boom"))
    queued = await _queue(session_factory, ProgressReporter(h.github, "check_run", AppConfig()), HEAD)
    async with session_factory() as session:
        with pytest.raises(RuntimeError):
            await run_review(
                session,
                queued.id,
                settings=SimpleNamespace(openai_api_key=""),
                github=h.github,
                jira=h.jira,
                publisher=h.publisher,
                codex_fn=h.codex,
                registry=h.registry,
            )
    async with session_factory() as session:
        stored = await session.get(ReviewRun, queued.id)
        assert stored.progress_status["state"] == "completed"
        assert stored.progress_status["conclusion"] == "failure"


@pytest.mark.asyncio
async def test_cleanup_marks_stale_rows_in_database(session_factory) -> None:
    from app.config import AppConfig
    from app.models import ReviewRun
    from app.services.progress_status import ProgressReporter, cleanup_stale
    from sqlalchemy import update

    fake = FakeGitHub()
    stale = await _queue(session_factory, ProgressReporter(fake, "commit_status", AppConfig()), HEAD)
    old = datetime.now(UTC) - timedelta(hours=3)
    async with session_factory() as session:
        run = await session.get(ReviewRun, stale.id)
        state = dict(run.progress_status, updated_at=old.isoformat())
        await session.execute(
            update(ReviewRun).where(ReviewRun.id == stale.id).values(created_at=old, progress_status=state)
        )
        await session.commit()

    ctx = MagicMock(id="org-a", config=AppConfig())
    ctx.github.kind = "pat"
    registry = MagicMock()
    registry.get.return_value = ctx
    with patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake):
        async with session_factory() as session:
            assert await cleanup_stale(session, registry, 900) == 1
        async with session_factory() as session:
            assert await cleanup_stale(session, registry, 900) == 0
    async with session_factory() as session:
        run = await session.get(ReviewRun, stale.id)
        assert run.progress_status["state"] == "completed"
        assert run.progress_status["conclusion"] == "failure"
    final = [call[2] for call in fake.calls if call[0] == "status"][-1]
    assert final["state"] == "failure"
