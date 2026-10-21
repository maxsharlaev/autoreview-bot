"""Progress status and the sensitive-content flag against Postgres (migration 008, issue #7)."""

from __future__ import annotations

import asyncio
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


# --- Write order around enqueue (review finding P2-1) ---


def _redis(side_effect):
    redis = MagicMock()
    redis.enqueue_job = AsyncMock(side_effect=side_effect)
    return redis


async def _queue_with(session_factory, reporter, redis):
    from app.services.review_enqueue import queue_review

    async with session_factory() as session:
        return await queue_review(
            session,
            redis,
            full_name="org/repo",
            number=7,
            pr_payload={"number": 7, "title": "ABC-12 validate", "head": {"ref": "feature/ABC-12"}},
            head_sha=HEAD,
            base_sha="b" * 40,
            is_fork=False,
            owner_id="org-a",
            progress=reporter,
        )


async def _only_run(session_factory):
    from app.models import ReviewRun
    from sqlalchemy import select

    async with session_factory() as session:
        return (await session.execute(select(ReviewRun))).scalars().one()


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_enqueue_failure_marks_run_failed_and_leaves_no_status(session_factory, mode) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    with pytest.raises(ConnectionError):
        await _queue_with(session_factory, ProgressReporter(fake, mode, AppConfig()), _redis(ConnectionError("redis")))

    run = await _only_run(session_factory)
    assert run.status == "failed"
    assert run.error_code == "QUEUE_UNAVAILABLE"
    assert run.progress_status is None
    assert fake.calls == []  # nothing was created on GitHub, so nothing can stay queued


def _worker(session_factory, h):
    from app.services.orchestrator import run_review

    async def work(run_id):
        async with session_factory() as session:
            return await run_review(
                session,
                run_id,
                settings=SimpleNamespace(openai_api_key=""),
                github=h.github,
                jira=h.jira,
                publisher=h.publisher,
                codex_fn=h.codex,
                registry=h.registry,
            )

    return work


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_worker_finishing_before_queued_write_is_not_clobbered(
    session_factory, monkeypatch, tmp_path, mode
) -> None:
    import uuid as _uuid

    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    kind = "app" if mode == "check_run" else "pat"
    h = Harness(monkeypatch, tmp_path, kind=kind)
    work = _worker(session_factory, h)

    async def enqueue_and_run(_name, run_id):
        # The fastest possible worker: the whole review finishes inside enqueue_job.
        await work(_uuid.UUID(run_id))
        return MagicMock(job_id="job-fast")

    await _queue_with(session_factory, ProgressReporter(h.github, mode, AppConfig()), _redis(enqueue_and_run))

    run = await _only_run(session_factory)
    assert run.status == "completed"
    assert run.progress_status["state"] == "completed"
    assert run.arq_job_id == "job-fast"
    if mode == "check_run":
        assert h.github.check_states() == ["in_progress", "completed"]
    else:
        assert [p["state"] for p in h.github.payloads("status")] == ["pending", "success"]
        assert not any(p["description"].startswith("The review is waiting") for p in h.github.payloads("status"))


class _SlowQueuedGitHub(FakeGitHub):
    """The API's queued write is slow; calls are recorded when they land on GitHub."""

    async def create_check_run(self, owner, repo, payload):
        if payload["status"] == "queued":
            await asyncio.sleep(0.5)
        self.calls.append(("create", payload))
        return 101 + len(self.payloads("create"))

    async def create_commit_status(self, owner, repo, sha, payload):
        if payload["description"].startswith("The review is waiting"):
            await asyncio.sleep(0.5)
        self.calls.append(("status", sha, payload))


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_worker_racing_the_queued_write_waits_and_reuses_it(session_factory, monkeypatch, tmp_path, mode) -> None:
    import uuid as _uuid

    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    kind = "app" if mode == "check_run" else "pat"
    slow = _SlowQueuedGitHub(info=None)
    h = Harness(monkeypatch, tmp_path, kind=kind, github=slow)
    work = _worker(session_factory, h)
    tasks = []

    async def enqueue_and_start(_name, run_id):
        # The worker starts while the API is still writing the queued status.
        tasks.append(asyncio.create_task(work(_uuid.UUID(run_id))))
        return MagicMock(job_id="job-race")

    await _queue_with(session_factory, ProgressReporter(slow, mode, AppConfig()), _redis(enqueue_and_start))
    result = await asyncio.wait_for(tasks[0], timeout=20)

    assert result.status == "completed"
    run = await _only_run(session_factory)
    assert run.progress_status["state"] == "completed"
    if mode == "check_run":
        assert len(slow.payloads("create")) == 1  # one check run, never a second queued one
        assert slow.check_states() == ["queued", "in_progress", "completed"]
        assert run.progress_status["check_run_id"] == 102
    else:
        assert [p["state"] for p in slow.payloads("status")] == ["pending", "pending", "success"]
        assert slow.payloads("status")[0]["description"].startswith("The review is waiting")


# --- Manual runs (API-triggered) ---


async def _queue_trigger(session_factory, reporter, redis, *, trigger, force, report_new_run=True):
    from app.services.review_enqueue import queue_review

    async with session_factory() as session:
        return await queue_review(
            session,
            redis,
            full_name="org/repo",
            number=7,
            pr_payload={"number": 7, "title": "ABC-12 validate", "head": {"ref": "feature/ABC-12"}},
            head_sha=HEAD,
            base_sha="b" * 40,
            is_fork=False,
            owner_id="org-a",
            trigger=trigger,
            force=force,
            progress=reporter,
            report_new_run=report_new_run,
        )


def _ok_redis():
    redis = MagicMock()
    redis.enqueue_job = AsyncMock(side_effect=lambda *_a: MagicMock(job_id=f"job-{uuid.uuid4().hex[:6]}"))
    return redis


async def _runs(session_factory):
    from app.models import ReviewRun
    from sqlalchemy import select

    async with session_factory() as session:
        return (await session.execute(select(ReviewRun).order_by(ReviewRun.created_at))).scalars().all()


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_manual_rerun_on_same_sha_replaces_open_webhook_status(session_factory, mode) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, mode, AppConfig())
    with patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=True)):
        hook = await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="webhook", force=False)
        manual = await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="manual", force=True)

    old, new = await _runs(session_factory)
    assert (old.id, new.id) == (hook.id, manual.id)
    assert old.status == "cancelled" and old.progress_status["conclusion"] == "cancelled"
    assert new.trigger == "manual" and new.progress_status["state"] == "queued"
    open_runs = [r for r in (old, new) if r.progress_status["state"] in ps_open()]
    assert open_runs == [new]  # never two open statuses on one commit
    if mode == "check_run":
        assert [c[0] for c in fake.calls] == ["create", "update", "create"]
        assert fake.calls[1][2]["conclusion"] == "cancelled"
    else:
        assert [c[2]["state"] for c in fake.calls] == ["pending", "error", "pending"]


def ps_open():
    from app.services.progress_status import OPEN_STATES

    return OPEN_STATES


@pytest.mark.asyncio
async def test_webhook_on_same_sha_reuses_open_manual_run_without_new_status(session_factory) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    with patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=True)):
        manual = await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="manual", force=True)
        hook = await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="webhook", force=False)
    assert hook.id == manual.id
    assert [c[0] for c in fake.calls] == ["create"]
    (run,) = await _runs(session_factory)
    assert run.progress_status["state"] == "queued"


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_manual_enqueue_failure_leaves_no_status(session_factory, mode) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    with pytest.raises(ConnectionError):
        await _queue_trigger(
            session_factory,
            ProgressReporter(fake, mode, AppConfig()),
            _redis(ConnectionError("redis")),
            trigger="manual",
            force=True,
        )
    (run,) = await _runs(session_factory)
    assert run.status == "failed" and run.error_code == "QUEUE_UNAVAILABLE"
    assert run.progress_status is None and fake.calls == []


@pytest.mark.asyncio
async def test_manual_opt_out_still_cancels_previous_open_status(session_factory) -> None:
    from app.config import AppConfig
    from app.services.progress_status import PROGRESS_OPT_OUT, ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    with patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=True)):
        await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="webhook", force=False)
        await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="manual", force=True, report_new_run=False)
    old, new = await _runs(session_factory)
    assert old.progress_status["conclusion"] == "cancelled"
    assert new.progress_status is None and new.summary[PROGRESS_OPT_OUT] is True
    assert [c[0] for c in fake.calls] == ["create", "update"]


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_manual_worker_racing_queued_write_reuses_it(session_factory, monkeypatch, tmp_path, mode) -> None:
    import uuid as _uuid

    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    kind = "app" if mode == "check_run" else "pat"
    slow = _SlowQueuedGitHub(info=None)
    h = Harness(monkeypatch, tmp_path, kind=kind, github=slow)
    work = _worker(session_factory, h)
    tasks = []

    async def enqueue_and_start(_name, run_id):
        tasks.append(asyncio.create_task(work(_uuid.UUID(run_id))))
        return MagicMock(job_id="job-manual")

    await _queue_trigger(
        session_factory,
        ProgressReporter(slow, mode, AppConfig()),
        _redis(enqueue_and_start),
        trigger="manual",
        force=True,
    )
    result = await asyncio.wait_for(tasks[0], timeout=20)
    assert result.status == "completed" and result.trigger == "manual"
    if mode == "check_run":
        assert len(slow.payloads("create")) == 1
        assert slow.check_states() == ["queued", "in_progress", "completed"]
    else:
        assert [p["state"] for p in slow.payloads("status")] == ["pending", "pending", "success"]


@pytest.mark.asyncio
async def test_rerun_after_lost_job_closes_its_open_status(session_factory) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="webhook", force=False)
    with patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=False)):
        await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="manual", force=True)
    old, new = await _runs(session_factory)
    assert old.status == "failed"
    assert old.progress_status["state"] == "completed" and old.progress_status["conclusion"] == "failure"
    assert new.progress_status["state"] == "queued"
    assert [c[0] for c in fake.calls] == ["create", "update", "create"]
