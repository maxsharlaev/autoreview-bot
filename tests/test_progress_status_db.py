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
    assert h.github.check_states() == ["queued", *["in_progress"] * 4, "completed"]


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

    async def enqueue_and_run(_name, run_id, **_kw):
        # The fastest possible worker: the whole review finishes inside enqueue_job.
        await work(_uuid.UUID(run_id))
        return MagicMock(job_id="job-fast")

    await _queue_with(session_factory, ProgressReporter(h.github, mode, AppConfig()), _redis(enqueue_and_run))

    run = await _only_run(session_factory)
    assert run.status == "completed"
    assert run.progress_status["state"] == "completed"
    assert run.arq_job_id == "job-fast"
    if mode == "check_run":
        assert h.github.check_states() == [*["in_progress"] * 4, "completed"]
    else:
        assert [p["state"] for p in h.github.payloads("status")] == [*["pending"] * 4, "success"]
        assert not any(p["description"].endswith("Queued, waiting for a worker…") for p in h.github.payloads("status"))


class _SlowQueuedGitHub(FakeGitHub):
    """The API's queued write is slow; calls are recorded when they land on GitHub."""

    async def create_check_run(self, owner, repo, payload):
        if payload["status"] == "queued":
            await asyncio.sleep(0.5)
        self.calls.append(("create", payload))
        return 101 + len(self.payloads("create"))

    async def create_commit_status(self, owner, repo, sha, payload):
        if payload["description"].endswith("Queued, waiting for a worker…"):
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

    async def enqueue_and_start(_name, run_id, **_kw):
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
        assert slow.check_states() == ["queued", *["in_progress"] * 4, "completed"]
        assert run.progress_status["check_run_id"] == 102
    else:
        assert [p["state"] for p in slow.payloads("status")] == [*["pending"] * 5, "success"]
        assert slow.payloads("status")[0]["description"].endswith("Queued, waiting for a worker…")


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
    redis.enqueue_job = AsyncMock(side_effect=lambda *_a, **_kw: MagicMock(job_id=f"job-{uuid.uuid4().hex[:6]}"))
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


@pytest.mark.parametrize("order", ["api_first", "worker_first"])
@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_manual_worker_racing_queued_write_reuses_it(session_factory, monkeypatch, tmp_path, mode, order) -> None:
    """The worker starts while the API is still about to write the queued status.

    Both orders are legitimate and are pinned explicitly here (no timing):
    - api_first: the queued status lands first and the worker reuses it;
    - worker_first: the worker claims the run first, so the API skips its queued write.
    Either way there is one run, one enqueue, one status, and nothing after completion.
    """
    import uuid as _uuid

    from app.config import AppConfig
    from app.models import ReviewRun
    from app.services import review_enqueue
    from app.services.progress_status import ProgressReporter
    from sqlalchemy import select

    kind = "app" if mode == "check_run" else "pat"
    slow = _SlowQueuedGitHub(info=None)
    h = Harness(monkeypatch, tmp_path, kind=kind, github=slow)
    work = _worker(session_factory, h)
    tasks = []
    queued_done = asyncio.Event()
    real_report_queued = review_enqueue._report_queued

    async def until_claimed(run_id):
        while True:
            async with session_factory() as session:
                status = (await session.execute(select(ReviewRun.status).where(ReviewRun.id == run_id))).scalar_one()
            if status != "pending":
                return
            await asyncio.sleep(0.01)

    async def report_queued(session, progress, run, full_name):
        if order == "worker_first":
            await asyncio.wait_for(until_claimed(run.id), timeout=10)
        try:
            await real_report_queued(session, progress, run, full_name)
        finally:
            queued_done.set()

    async def start_worker(run_id):
        if order == "api_first":
            await asyncio.wait_for(queued_done.wait(), timeout=10)
        return await work(run_id)

    async def enqueue_and_start(_name, run_id, **_kw):
        tasks.append(asyncio.create_task(start_worker(_uuid.UUID(run_id))))
        return MagicMock(job_id="job-manual")

    redis = _redis(enqueue_and_start)
    with patch("app.services.review_enqueue._report_queued", report_queued):
        await _queue_trigger(
            session_factory,
            ProgressReporter(slow, mode, AppConfig()),
            redis,
            trigger="manual",
            force=True,
        )
    result = await asyncio.wait_for(tasks[0], timeout=20)
    assert result.status == "completed" and result.trigger == "manual"
    assert len(tasks) == 1 and redis.enqueue_job.await_count == 1
    assert len(await _runs(session_factory)) == 1
    if mode == "check_run":
        assert len(slow.payloads("create")) == 1  # one check run, reused by the worker
        expected = (
            ["queued", *["in_progress"] * 4, "completed"]
            if order == "api_first"
            else [*["in_progress"] * 4, "completed"]
        )
        assert slow.check_states() == expected
    else:
        expected = [*["pending"] * 5, "success"] if order == "api_first" else [*["pending"] * 4, "success"]
        assert [p["state"] for p in slow.payloads("status")] == expected


@pytest.mark.asyncio
async def test_rerun_after_lost_job_closes_its_open_status(session_factory) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="webhook", force=False)
    with (
        patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=False)),
        patch("app.services.review_enqueue.ENQUEUE_GRACE_SECONDS", 0.0),  # the job is lost, not just slow
    ):
        await _queue_trigger(session_factory, reporter, _ok_redis(), trigger="manual", force=True)
    old, new = await _runs(session_factory)
    assert old.status == "failed"
    assert old.progress_status["state"] == "completed" and old.progress_status["conclusion"] == "failure"
    assert new.progress_status["state"] == "queued"
    assert [c[0] for c in fake.calls] == ["create", "update", "create"]


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_stale_manual_head_sha_leaves_open_run_untouched(session_factory, mode) -> None:
    from app.api.deps import get_session
    from app.config import AppConfig
    from app.owners.registry import RouteResult
    from app.services.progress_status import ProgressReporter
    from tests.test_manual_review import _client
    from tests.test_progress_status import _info

    fake = FakeGitHub(info=_info())  # current head: HEAD
    kind = "app" if mode == "check_run" else "pat"
    await _queue_trigger(
        session_factory, ProgressReporter(fake, mode, AppConfig()), _ok_redis(), trigger="webhook", force=False
    )
    calls_before = list(fake.calls)
    (before,) = await _runs(session_factory)

    async def real_session():
        async with session_factory() as session:
            yield session

    client = _client(with_api_key=True)
    client.app.dependency_overrides[get_session] = real_session
    redis = _ok_redis()
    client.app.state.redis = redis
    ctx = SimpleNamespace(id="org-a", config=AppConfig(), github=SimpleNamespace(kind=kind))
    registry = MagicMock()
    registry.get.return_value = ctx
    registry.resolve.return_value = RouteResult("org-a", "exact")
    with (
        patch("app.api.v1.reviews.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake),
        patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=True)),
    ):
        response = client.post(
            "/api/v1/reviews", json={"repository": "org/repo", "number": 7, "head_sha": "0" * 40, "force": True}
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_head_sha"
    assert response.json()["detail"]["current_head_sha"] == HEAD
    (after,) = await _runs(session_factory)  # no new row
    assert after.id == before.id and after.status == "pending"
    assert after.progress_status == before.progress_status
    redis.enqueue_job.assert_not_awaited()
    assert fake.calls == calls_before  # no status write, no cancellation


# --- Manual read/enqueue race (review finding P2 on #15) ---

OLDER = "1" * 40


def _info_at(sha: str):
    import dataclasses

    from tests.test_progress_status import _info

    return dataclasses.replace(_info(), head_sha=sha)


def _manual_client(session_factory, fake, kind):
    from app.api.deps import get_session
    from app.config import AppConfig
    from app.owners.registry import RouteResult
    from tests.test_manual_review import _client

    async def real_session():
        async with session_factory() as session:
            yield session

    client = _client(with_api_key=True)
    client.app.dependency_overrides[get_session] = real_session
    redis = _ok_redis()
    client.app.state.redis = redis
    ctx = SimpleNamespace(id="org-a", config=AppConfig(), github=SimpleNamespace(kind=kind))
    registry = MagicMock()
    registry.get.return_value = ctx
    registry.resolve.return_value = RouteResult("org-a", "exact")
    patches = (
        patch("app.api.v1.reviews.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake),
        patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=True)),
    )
    return client, redis, patches


async def _post_manual(client, patches, **body):
    import httpx

    # In-process ASGI call on this event loop (the DB engine is bound to it).
    transport = httpx.ASGITransport(app=client.app)
    with patches[0], patches[1], patches[2]:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.post(
                "/api/v1/reviews", json={"repository": "org/repo", "number": 7, "force": True, **body}
            )


async def _snapshot(session_factory):
    return [(r.id, r.head_sha, r.status, r.progress_status) for r in await _runs(session_factory)]


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_manual_read_then_webhook_push_then_enqueue_is_rejected(session_factory, mode) -> None:
    """Manual reads head A; webhook B is queued meanwhile; the manual enqueue must not cancel B."""
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    kind = "app" if mode == "check_run" else "pat"
    fake = FakeGitHub()
    reporter = ProgressReporter(fake, mode, AppConfig())
    reads = []
    webhook_done = asyncio.Event()

    async def get_pull_request(owner, repo, number):
        reads.append(number)
        if len(reads) == 1:
            return _info_at(HEAD)  # manual request reads A
        return _info_at(NEW_HEAD)  # the re-read under the lock sees B

    fake.get_pull_request = AsyncMock(side_effect=get_pull_request)
    client, redis, patches = _manual_client(session_factory, fake, kind)

    # Webhook B is processed between the manual read and the manual enqueue: queue it right
    # after the first read by wrapping queue_review in the endpoint.
    from app.api.v1 import reviews

    real_queue = reviews.queue_review

    async def queue_after_webhook(*args, **kwargs):
        if not webhook_done.is_set():
            await _queue_trigger_sha(session_factory, reporter, NEW_HEAD)
            webhook_done.set()
        return await real_queue(*args, **kwargs)

    with patch("app.api.v1.reviews.queue_review", queue_after_webhook):
        response = await _post_manual(client, patches, head_sha=HEAD)

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "stale_head_sha", "head_sha": HEAD, "current_head_sha": NEW_HEAD}
    assert len(reads) == 2
    runs = await _runs(session_factory)
    assert [(r.head_sha, r.status, r.trigger) for r in runs] == [(NEW_HEAD, "pending", "webhook")]
    assert runs[0].progress_status["state"] == "queued"
    redis.enqueue_job.assert_not_awaited()  # the endpoint's redis; webhook B used its own
    # Only webhook B's queued status was written; the manual request wrote nothing.
    assert len(fake.calls) == 1


async def _queue_trigger_sha(session_factory, reporter, sha, *, trigger="webhook", force=False, current_head=None):
    from app.services.review_enqueue import queue_review

    async with session_factory() as session:
        return await queue_review(
            session,
            _ok_redis(),
            full_name="org/repo",
            number=7,
            pr_payload={"number": 7, "title": "ABC-12 validate", "head": {"ref": "feature/ABC-12"}},
            head_sha=sha,
            base_sha="b" * 40,
            is_fork=False,
            owner_id="org-a",
            trigger=trigger,
            force=force,
            progress=reporter,
            current_head=current_head,
        )


@pytest.mark.parametrize("mode", ["check_run", "commit_status"])
@pytest.mark.asyncio
async def test_manual_cancels_older_run_when_head_is_still_requested_sha(session_factory, mode) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    kind = "app" if mode == "check_run" else "pat"
    fake = FakeGitHub()
    fake.get_pull_request = AsyncMock(return_value=_info_at(HEAD))
    await _queue_trigger_sha(session_factory, ProgressReporter(fake, mode, AppConfig()), OLDER)
    client, redis, patches = _manual_client(session_factory, fake, kind)
    response = await _post_manual(client, patches, head_sha=HEAD)

    assert response.status_code == 202
    assert fake.get_pull_request.await_count == 2  # request read + re-read under the lock
    old, new = await _runs(session_factory)
    assert old.head_sha == OLDER and old.status == "cancelled"
    assert old.progress_status["conclusion"] == "cancelled"
    assert new.head_sha == HEAD and new.trigger == "manual" and new.progress_status["state"] == "queued"
    redis.enqueue_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_without_other_open_run_reads_github_once(session_factory) -> None:
    from app.models import ReviewRun
    from sqlalchemy import update

    fake = FakeGitHub()
    fake.get_pull_request = AsyncMock(return_value=_info_at(HEAD))
    # The PR is known and has a finished run on an older SHA: nothing open to supersede.
    older = await _queue_trigger_sha(session_factory, None, OLDER)
    async with session_factory() as session:
        await session.execute(update(ReviewRun).where(ReviewRun.id == older.id).values(status="completed"))
        await session.commit()
    client, redis, patches = _manual_client(session_factory, fake, "app")
    response = await _post_manual(client, patches)
    assert response.status_code == 202
    assert fake.get_pull_request.await_count == 1


@pytest.mark.parametrize("failure", ["error", "timeout"])
@pytest.mark.asyncio
async def test_manual_head_recheck_failure_changes_nothing(session_factory, monkeypatch, failure) -> None:
    from app.adapters.github import GitHubError
    from app.config import AppConfig
    from app.services import review_enqueue
    from app.services.progress_status import ProgressReporter

    monkeypatch.setattr(review_enqueue, "HEAD_CHECK_TIMEOUT_SECONDS", 0.2)
    fake = FakeGitHub()
    await _queue_trigger_sha(session_factory, ProgressReporter(fake, "check_run", AppConfig()), OLDER)
    snapshot, calls = await _snapshot(session_factory), list(fake.calls)

    async def second_read_fails(owner, repo, number):
        if fake.get_pull_request.await_count == 1:
            return _info_at(HEAD)
        if failure == "timeout":
            await asyncio.sleep(5)
        raise GitHubError("503 unavailable", status_code=503)

    fake.get_pull_request = AsyncMock(side_effect=second_read_fails)
    client, redis, patches = _manual_client(session_factory, fake, "app")
    response = await _post_manual(client, patches, head_sha=HEAD)

    assert response.status_code == 502
    assert await _snapshot(session_factory) == snapshot
    assert fake.calls == calls
    redis.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_per_pr_lock_serializes_head_recheck_and_webhook(session_factory) -> None:
    """While a manual enqueue re-reads the head under the lock, a webhook for the same PR waits."""
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    await _queue_trigger_sha(session_factory, reporter, OLDER)
    in_check, release = asyncio.Event(), asyncio.Event()

    async def slow_head():
        in_check.set()
        await release.wait()
        return HEAD

    manual = asyncio.create_task(
        _queue_trigger_sha(session_factory, reporter, HEAD, trigger="manual", force=True, current_head=slow_head)
    )
    await asyncio.wait_for(in_check.wait(), timeout=5)
    webhook = asyncio.create_task(_queue_trigger_sha(session_factory, reporter, NEW_HEAD))
    try:
        await asyncio.sleep(0.5)
        blocked = not webhook.done()  # waits on the per-PR lock held by the manual enqueue
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(manual, webhook, return_exceptions=True), timeout=10)
    assert blocked

    by_sha = {r.head_sha: r for r in await _runs(session_factory)}
    assert by_sha[OLDER].status == "cancelled"
    assert by_sha[HEAD].status == "cancelled"  # superseded by the newer webhook, in order
    assert by_sha[NEW_HEAD].status == "pending"


# --- Bounded waits around the per-PR lock (review finding on #15) ---


@pytest.mark.asyncio
async def test_hung_abort_runs_after_unlock_and_does_not_block_the_next_request(session_factory, monkeypatch) -> None:
    from app.config import AppConfig
    from app.services import review_enqueue
    from app.services.progress_status import ProgressReporter

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    await _queue_trigger_sha(session_factory, reporter, OLDER)  # open run with a job id
    abort_started, release = asyncio.Event(), asyncio.Event()

    async def hung_abort(redis, job_id, timeout=None):
        abort_started.set()
        await release.wait()

    monkeypatch.setattr(review_enqueue, "abort_job", hung_abort)
    monkeypatch.setattr(review_enqueue, "job_is_active", AsyncMock(return_value=True))
    first = asyncio.create_task(_queue_trigger_sha(session_factory, reporter, HEAD, trigger="manual", force=True))
    try:
        await asyncio.wait_for(abort_started.wait(), timeout=5)
        # The superseding run is already committed and the old one cancelled while abort hangs.
        by_sha = {r.head_sha: r for r in await _runs(session_factory)}
        assert by_sha[OLDER].status == "cancelled" and by_sha[HEAD].status == "pending"
        # A second request for the same PR is not blocked by the hung abort.
        second = await asyncio.wait_for(_queue_trigger_sha(session_factory, reporter, HEAD), timeout=5)
        assert second.id == by_sha[HEAD].id  # reuses the open run on the same SHA
        assert not first.done()
    finally:
        release.set()
        await asyncio.wait_for(first, timeout=5)


@pytest.mark.asyncio
async def test_lock_wait_is_bounded_and_changes_nothing(session_factory, monkeypatch) -> None:
    from app.config import AppConfig
    from app.services import review_enqueue
    from app.services.progress_status import ProgressReporter
    from app.services.review_enqueue import QueueBusyError
    from sqlalchemy import func, select

    fake = FakeGitHub()
    reporter = ProgressReporter(fake, "check_run", AppConfig())
    await _queue_trigger_sha(session_factory, reporter, OLDER)
    snapshot, calls = await _snapshot(session_factory), list(fake.calls)
    monkeypatch.setattr(review_enqueue, "LOCK_WAIT_SECONDS", 0.5)
    loop = asyncio.get_running_loop()
    async with session_factory() as holder:
        key = func.hashtextextended("review:org/repo#7", 0)
        assert (await holder.execute(select(func.pg_try_advisory_xact_lock(key)))).scalar()
        started = loop.time()
        with pytest.raises(QueueBusyError):
            await _queue_trigger_sha(session_factory, reporter, HEAD, trigger="manual", force=True)
        waited = loop.time() - started
        await holder.rollback()
    assert 0.4 <= waited < 3
    assert await _snapshot(session_factory) == snapshot
    assert fake.calls == calls
    # Once the lock is free the same request goes through.
    run = await _queue_trigger_sha(session_factory, reporter, HEAD, trigger="manual", force=True)
    assert run.head_sha == HEAD


@pytest.mark.asyncio
async def test_manual_endpoint_answers_503_when_pr_lock_is_busy(session_factory, monkeypatch) -> None:
    from app.services import review_enqueue
    from sqlalchemy import func, select

    monkeypatch.setattr(review_enqueue, "LOCK_WAIT_SECONDS", 0.3)
    fake = FakeGitHub()
    fake.get_pull_request = AsyncMock(return_value=_info_at(HEAD))
    client, redis, patches = _manual_client(session_factory, fake, "app")
    async with session_factory() as holder:
        await holder.execute(select(func.pg_try_advisory_xact_lock(func.hashtextextended("review:org/repo#7", 0))))
        response = await _post_manual(client, patches)
        await holder.rollback()
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "pr_queue_busy"}
    assert response.headers["retry-after"] == "5"
    assert await _runs(session_factory) == []
    redis.enqueue_job.assert_not_awaited()
    assert fake.calls == []


# --- commit -> enqueue window -------------------------------------------------------------


def _paused_enqueue():
    """enqueue_review stand-in whose first call waits until released; records every call."""
    from app.queue import review_job_id

    calls: list[str] = []
    reached, release = asyncio.Event(), asyncio.Event()

    async def enqueue(_redis, run_id):
        calls.append(run_id)
        if len(calls) == 1:
            reached.set()
            await release.wait()
        return review_job_id(run_id)

    return enqueue, calls, reached, release


@pytest.mark.parametrize("trigger", ["webhook", "manual"])
@pytest.mark.asyncio
async def test_second_request_in_enqueue_window_reuses_pending_run(session_factory, trigger) -> None:
    from app.queue import review_job_id

    enqueue, calls, reached, release = _paused_enqueue()
    with (
        patch("app.services.review_enqueue.enqueue_review", enqueue),
        # The first job is not in Redis yet: exactly the window the grace period covers.
        patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=False)),
    ):
        first = asyncio.create_task(_queue_trigger(session_factory, None, _ok_redis(), trigger="webhook", force=False))
        try:
            await asyncio.wait_for(reached.wait(), timeout=10)
            # The first run is committed (lock released) but not enqueued yet.
            [pending] = await _runs(session_factory)
            assert pending.status == "pending" and pending.arq_job_id == review_job_id(str(pending.id))
            second = await _queue_trigger(session_factory, None, _ok_redis(), trigger=trigger, force=False)
        finally:
            release.set()
        first_run = await asyncio.wait_for(first, timeout=10)
    assert second.id == first_run.id
    assert calls == [str(first_run.id)]  # no second enqueue
    [only] = await _runs(session_factory)
    assert only.status == "pending" and only.arq_job_id == review_job_id(str(only.id))


@pytest.mark.asyncio
async def test_force_in_enqueue_window_replaces_and_aborts_deterministic_job(session_factory) -> None:
    from app.queue import review_job_id

    enqueue, calls, reached, release = _paused_enqueue()
    abort = AsyncMock()
    with (
        patch("app.services.review_enqueue.enqueue_review", enqueue),
        patch("app.services.review_enqueue.job_is_active", AsyncMock(return_value=False)),
        patch("app.services.review_enqueue.abort_job", abort),
    ):
        first = asyncio.create_task(_queue_trigger(session_factory, None, _ok_redis(), trigger="webhook", force=False))
        try:
            await asyncio.wait_for(reached.wait(), timeout=10)
            second = await _queue_trigger(session_factory, None, _ok_redis(), trigger="manual", force=True)
        finally:
            release.set()
        first_run = await asyncio.wait_for(first, timeout=10)
    old, new = await _runs(session_factory)
    assert old.id == first_run.id and new.id == second.id
    assert old.status == "cancelled" and new.status == "pending"
    assert calls == [str(old.id), str(new.id)]
    # The superseded job id is known before it reaches Redis, so the abort targets it.
    abort.assert_awaited_once()
    assert abort.await_args.args[1] == review_job_id(str(old.id))


@pytest.mark.asyncio
async def test_failed_enqueue_is_replaced_within_grace(session_factory) -> None:
    from app.services.constants import QUEUE_UNAVAILABLE

    broken = MagicMock()
    broken.enqueue_job = AsyncMock(side_effect=ConnectionError("redis down"))
    with pytest.raises(ConnectionError):
        await _queue_trigger(session_factory, None, broken, trigger="webhook", force=False)
    redis = _ok_redis()
    retry = await _queue_trigger(session_factory, None, redis, trigger="webhook", force=False)
    old, new = await _runs(session_factory)
    assert old.status == "failed" and old.error_code == QUEUE_UNAVAILABLE
    assert new.id == retry.id and new.status == "pending"
    redis.enqueue_job.assert_awaited_once()
    assert redis.enqueue_job.await_args.kwargs["_job_id"] == f"review:{new.id}"


@pytest.mark.parametrize("status", ["failed", "cancelled", "running", "completed"])
@pytest.mark.asyncio
async def test_worker_refuses_non_pending_run(session_factory, monkeypatch, tmp_path, status) -> None:
    from app.models import ReviewRun
    from app.services.orchestrator import run_review
    from sqlalchemy import update

    run = await _queue_trigger(session_factory, None, _ok_redis(), trigger="webhook", force=False)
    async with session_factory() as session:
        await session.execute(update(ReviewRun).where(ReviewRun.id == run.id).values(status=status))
        await session.commit()
    github, codex = MagicMock(), AsyncMock()
    async with session_factory() as session:
        result = await run_review(
            session,
            str(run.id),
            settings=SimpleNamespace(openai_api_key=""),
            github=github,
            codex_fn=codex,
            registry=Harness(monkeypatch, tmp_path).registry,
        )
    assert result.status == status
    assert github.mock_calls == [] and codex.await_count == 0
    [row] = await _runs(session_factory)
    assert row.status == status


@pytest.mark.asyncio
async def test_worker_losing_the_pending_claim_skips(session_factory, monkeypatch, tmp_path) -> None:
    from app.models import ReviewRun
    from app.services.orchestrator import run_review
    from sqlalchemy import update

    run = await _queue_trigger(session_factory, None, _ok_redis(), trigger="webhook", force=False)
    h = Harness(monkeypatch, tmp_path)
    async with session_factory() as session:
        real_execute = session.execute

        raced: list[bool] = []

        async def racing_execute(stmt, *a, **kw):
            result = await real_execute(stmt, *a, **kw)
            if not raced:
                # A competing job claims the run right after this worker read it as pending.
                raced.append(True)
                async with session_factory() as other:
                    await other.execute(update(ReviewRun).where(ReviewRun.id == run.id).values(status="running"))
                    await other.commit()
            return result

        session.execute = racing_execute
        await run_review(
            session,
            str(run.id),
            settings=SimpleNamespace(openai_api_key=""),
            github=h.github,
            publisher=h.publisher,
            codex_fn=h.codex,
            registry=h.registry,
        )
    # No paid model call and no publication: the competing job owns the run.
    assert h.codex.await_count == 0 and h.publisher.publish.await_count == 0
    [row] = await _runs(session_factory)
    assert row.status == "running" and row.error_code is None


def _owner_skip_path(h, path):
    from app.owners.registry import RouteResult

    if path == "owner_disabled":
        h.registry.get.return_value = None
        h.registry.is_disabled.return_value = True
        h.registry.disabled_status_context.return_value = h.ctx
    elif path == "owner_not_configured":
        h.registry.get.return_value = None
        h.registry.is_disabled.return_value = False
        h.registry.disabled_status_context.return_value = None
    elif path == "route_disabled":  # re-resolve rejects the route: owner disabled
        h.registry.resolve.return_value = RouteResult("", "owner_disabled")
    elif path == "route_rejected":  # re-resolve rejects the route for another reason
        h.registry.resolve.return_value = RouteResult("", "repo_not_allowed")
    else:  # owner_changed: the repository now routes to another owner
        h.registry.resolve.return_value = RouteResult("org-b", "exact")


async def _owner_skip_review(session_factory, h, run_id, *, held_by_other_job):
    from app.models import ReviewRun
    from app.services.orchestrator import run_review
    from sqlalchemy import update

    async with session_factory() as session:
        real_execute = session.execute
        raced: list[bool] = []

        async def racing_execute(stmt, *a, **kw):
            result = await real_execute(stmt, *a, **kw)
            if held_by_other_job and not raced:
                # Another job claims the run right after this one read it as pending.
                raced.append(True)
                async with session_factory() as other:
                    await other.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(status="running"))
                    await other.commit()
            return result

        session.execute = racing_execute
        return await run_review(
            session,
            str(run_id),
            settings=SimpleNamespace(openai_api_key=""),
            github=h.github,
            publisher=h.publisher,
            codex_fn=h.codex,
            registry=h.registry,
        )


@pytest.mark.parametrize(
    "path", ["owner_disabled", "owner_not_configured", "owner_changed", "route_disabled", "route_rejected"]
)
@pytest.mark.asyncio
async def test_owner_skip_leaves_run_held_by_another_job_alone(session_factory, monkeypatch, tmp_path, path) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    h = Harness(monkeypatch, tmp_path)
    run = await _queue_trigger(
        session_factory,
        ProgressReporter(h.github, "check_run", AppConfig()),
        _ok_redis(),
        trigger="webhook",
        force=False,
    )
    queued_calls = list(h.github.calls)
    _owner_skip_path(h, path)
    result = await _owner_skip_review(session_factory, h, run.id, held_by_other_job=True)
    [row] = await _runs(session_factory)
    assert row.status == "running" and row.skip_reason is None and result.status == "running"
    assert h.github.calls == queued_calls  # the other job owns the status: nothing written
    assert h.codex.await_count == 0 and h.publisher.publish.await_count == 0


@pytest.mark.parametrize(
    "path", ["owner_disabled", "owner_not_configured", "owner_changed", "route_disabled", "route_rejected"]
)
@pytest.mark.asyncio
async def test_owner_skip_of_pending_run_still_skips(session_factory, monkeypatch, tmp_path, path) -> None:
    from app.config import AppConfig
    from app.services.progress_status import ProgressReporter

    h = Harness(monkeypatch, tmp_path)
    run = await _queue_trigger(
        session_factory,
        ProgressReporter(h.github, "check_run", AppConfig()),
        _ok_redis(),
        trigger="webhook",
        force=False,
    )
    _owner_skip_path(h, path)
    result = await _owner_skip_review(session_factory, h, run.id, held_by_other_job=False)
    [row] = await _runs(session_factory)
    expected = {"route_disabled": "owner_disabled", "route_rejected": "owner_changed"}.get(path, path)
    assert row.status == "skipped" and row.skip_reason == expected and result.status == "skipped"
    if path == "owner_disabled":
        # The disabled owner's queued status is closed as skipped with its own credentials.
        assert h.github.check_states() == ["queued", "completed"]
        assert h.github.calls[-1][2]["conclusion"] == "skipped"
