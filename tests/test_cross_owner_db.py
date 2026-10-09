"""Real DB-backed tests for cross-owner data isolation.

These tests verify that:
1. A reviews a SHA with finding X, then B reviews same SHA - B gets new run, no A finding text
2. B->A back - A gets fresh run, not old completed run
3. Legacy 'default' findings map correctly when switching from Mode A to C

Requires TEST_DATABASE_URL environment variable pointing to a Postgres database
ending with '_test'.
"""

from __future__ import annotations

import os
import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


def _validate_test_database_url() -> str | None:
    """Validate TEST_DATABASE_URL and return a reason if invalid."""
    if not TEST_DB_URL:
        return "TEST_DATABASE_URL not set"
    match = re.search(r"/([^/?]+)(?:\?|$)", TEST_DB_URL)
    if not match:
        return "Could not parse database name from TEST_DATABASE_URL"
    db_name = match.group(1)
    if not db_name.endswith("_test"):
        return f"Database name must end with '_test' (got: {db_name})"
    return None


_skip_reason = _validate_test_database_url()
requires_test_postgres = pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "")


def _get_asyncpg_url() -> str:
    """Convert TEST_DATABASE_URL to asyncpg format."""
    url = TEST_DB_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _get_alembic_config():
    """Get Alembic config pointing to the test database."""
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", _get_asyncpg_url())
    config.attributes["use_test_url"] = True
    config.attributes["configure_logger"] = False
    return config


def _run_migrations(config, target: str) -> None:
    """Run alembic migrations."""
    import asyncio

    from alembic import command

    def run_sync():
        command.upgrade(config, target)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        run_sync()
        return
    import concurrent.futures

    # Alembic's env.py calls asyncio.run(); run it off the test's event loop.
    with concurrent.futures.ThreadPoolExecutor() as executor:
        executor.submit(run_sync).result()


def _downgrade_migrations(config, target: str) -> None:
    """Run alembic downgrade."""
    import asyncio

    from alembic import command

    def run_sync():
        command.downgrade(config, target)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        run_sync()
        return
    import concurrent.futures

    # Alembic's env.py calls asyncio.run(); run it off the test's event loop.
    with concurrent.futures.ThreadPoolExecutor() as executor:
        executor.submit(run_sync).result()


HEAD_SHA = "c" * 40
BASE_SHA = "b" * 40

_CLEAR_FINDINGS_SQL = """
DO $$
BEGIN
    IF to_regclass('public.findings') IS NOT NULL THEN
        TRUNCATE findings CASCADE;
    END IF;
END $$;
"""


async def _clear_findings() -> None:
    """Findings shared by several owners block the 007 downgrade by design; clear them first."""
    import asyncpg

    conn = await asyncpg.connect(_get_asyncpg_url().replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(_CLEAR_FINDINGS_SQL)
    finally:
        await conn.close()


def _recording_codex(prompts: list[str], *, stable_id: str, tag: str):
    """Fake codex_fn that records the prompt and returns one schema-valid finding."""
    import json

    async def fake_codex(*, checkout, prompt, schema_path, output_path, **_kwargs):
        prompts.append(prompt)
        result = {
            "schema_version": 1,
            "reviewed_head_sha": HEAD_SHA,
            "previous_reviewed_head_sha": None,
            "summary": f"{tag} summary",
            "task_alignment": {"issue_key": None, "status": "unclear", "unmet_acceptance_criteria": []},
            "previous_findings": [],
            "findings": [
                {
                    "id": stable_id,
                    "severity": "P1",
                    "confidence": 0.9,
                    "category": "security",
                    "path": "src/app.py",
                    "line": 10,
                    "title": f"{tag} title",
                    "scenario": f"{tag} scenario",
                    "evidence": f"{tag} evidence",
                    "recommendation": f"{tag} recommendation",
                    "blocking_candidate": True,
                }
            ],
            "coverage": {"files_total": 0, "files_reviewed": 0, "truncated": False, "skipped_paths": []},
        }
        output_path.write_text(json.dumps(result), encoding="utf-8")
        return json.dumps(result)

    return fake_codex


def _mock_github(full_name: str, number: int):
    """GitHub client double returning a private PR at HEAD_SHA."""
    from types import SimpleNamespace

    from app.adapters.github import PullRequestInfo

    owner, repo = full_name.split("/", 1)
    info = PullRequestInfo(
        owner=owner,
        repo=repo,
        number=number,
        full_name=full_name,
        html_url=f"https://github.com/{full_name}/pull/{number}",
        title="Test PR",
        body="Test body",
        state="open",
        draft=False,
        author="test-user",
        assignee=None,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        base_ref="main",
        head_ref="feature",
        is_fork=False,
        visibility="private",
        commits_count=1,
        additions=10,
        deletions=5,
        changed_files=2,
    )
    mock = AsyncMock()
    mock.get_pull_request = AsyncMock(return_value=info)
    mock.collaborator_permission = AsyncMock(return_value="write")
    mock.list_files = AsyncMock(return_value=[])
    mock.installation_token = AsyncMock(return_value="test-token")
    mock.comment_author_login = AsyncMock(return_value="bot[bot]")
    mock.upsert_sticky_comment = AsyncMock()
    mock._credentials = SimpleNamespace(kind="app")
    mock.previous_comment_authors = ()
    return mock


def _mock_registry(current_owner: dict[str, str], default_owner_id: str = "owner-a"):
    """Registry double with owner-a and owner-b; routing follows current_owner['id']."""
    from types import SimpleNamespace

    from app.config import AppConfig
    from app.owners.registry import ROUTE_EXACT, RouteResult

    def ctx(owner_id: str):
        return SimpleNamespace(
            id=owner_id,
            config=AppConfig(),
            openai_api_key="test-key",
            jira=None,
            slack=None,
            github=SimpleNamespace(kind="pat", token=f"ghp_{owner_id}"),
        )

    mock = MagicMock()
    mock.default_owner_id = default_owner_id
    mock.get.side_effect = lambda owner_id: ctx(owner_id) if owner_id in {"owner-a", "owner-b"} else None
    mock.is_disabled.return_value = False
    mock.resolve.side_effect = lambda *_args, **_kwargs: RouteResult(current_owner["id"], ROUTE_EXACT)
    return mock


@requires_test_postgres
class TestCrossOwnerReviewDB:
    """DB-backed tests for cross-owner review isolation."""

    @pytest.fixture(autouse=True)
    async def setup_and_teardown(self):
        """Set up test database with all migrations, run tests, then clean up."""
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        config = _get_alembic_config()
        await _clear_findings()
        _downgrade_migrations(config, "base")
        _run_migrations(config, "head")

        engine = create_async_engine(_get_asyncpg_url())
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self.session_factory = async_session

        async with async_session() as session:
            yield session

        await engine.dispose()
        await _clear_findings()
        _downgrade_migrations(config, "base")

    @pytest.mark.asyncio
    @respx.mock
    async def test_owner_a_reviews_then_owner_b_gets_fresh_run(self, setup_and_teardown):
        """A reviews SHA with finding X, then B reviews same SHA - B gets new run."""
        from app.models import Repository
        from app.services.review_enqueue import queue_review

        session = setup_and_teardown
        mock_redis = MagicMock()
        mock_redis.enqueue_job = AsyncMock(return_value=MagicMock(job_id="job-1"))

        # Create repository owned by A
        repo = Repository(full_name="org/test-repo", enabled=True, owner_id="owner-a")
        session.add(repo)
        await session.flush()

        # A queues a review
        run_a = await queue_review(
            session,
            mock_redis,
            full_name="org/test-repo",
            number=1,
            pr_payload={
                "number": 1,
                "html_url": "https://github.com/org/test-repo/pull/1",
                "title": "Test PR",
                "user": {"login": "author"},
                "state": "open",
                "head": {"sha": "sha123", "ref": "feature"},
                "base": {"sha": "base123"},
            },
            head_sha="sha123",
            base_sha="base123",
            is_fork=False,
            owner_id="owner-a",
        )
        assert run_a.owner_id == "owner-a"

        # Now B tries to queue for the same SHA
        # First, update repository owner to B
        repo.owner_id = "owner-b"
        await session.flush()

        run_b = await queue_review(
            session,
            mock_redis,
            full_name="org/test-repo",
            number=1,
            pr_payload={
                "number": 1,
                "html_url": "https://github.com/org/test-repo/pull/1",
                "title": "Test PR",
                "user": {"login": "author"},
                "state": "open",
                "head": {"sha": "sha123", "ref": "feature"},
                "base": {"sha": "base123"},
            },
            head_sha="sha123",
            base_sha="base123",
            is_fork=False,
            owner_id="owner-b",
        )

        # B should get a NEW run, not A's run
        assert run_b.id != run_a.id
        assert run_b.owner_id == "owner-b"

    @pytest.mark.asyncio
    @respx.mock
    async def test_owner_b_then_a_back_gets_fresh_run(self, setup_and_teardown):
        """B->A back: A should get fresh run, not reuse old completed run."""
        from app.models import Repository
        from app.services.review_enqueue import queue_review

        session = setup_and_teardown
        mock_redis = MagicMock()
        mock_redis.enqueue_job = AsyncMock(return_value=MagicMock(job_id="job-1"))

        # Create repository
        repo = Repository(full_name="org/swing-repo", enabled=True, owner_id="owner-a")
        session.add(repo)
        await session.flush()

        # A queues and completes a review
        run_a1 = await queue_review(
            session,
            mock_redis,
            full_name="org/swing-repo",
            number=1,
            pr_payload={
                "number": 1,
                "html_url": "https://github.com/org/swing-repo/pull/1",
                "title": "Test PR",
                "user": {"login": "author"},
                "state": "open",
                "head": {"sha": "sha456", "ref": "feature"},
                "base": {"sha": "base456"},
            },
            head_sha="sha456",
            base_sha="base456",
            is_fork=False,
            owner_id="owner-a",
        )
        # Mark A's run as completed
        run_a1.status = "completed"
        await session.commit()

        # B takes over and creates a run
        repo.owner_id = "owner-b"
        await session.flush()

        run_b = await queue_review(
            session,
            mock_redis,
            full_name="org/swing-repo",
            number=1,
            pr_payload={
                "number": 1,
                "html_url": "https://github.com/org/swing-repo/pull/1",
                "title": "Test PR",
                "user": {"login": "author"},
                "state": "open",
                "head": {"sha": "sha456", "ref": "feature"},
                "base": {"sha": "base456"},
            },
            head_sha="sha456",
            base_sha="base456",
            is_fork=False,
            owner_id="owner-b",
        )
        run_b.status = "completed"
        await session.commit()

        # A takes back ownership
        repo.owner_id = "owner-a"
        await session.flush()

        # A queues again - should NOT reuse old completed run_a1 because B has newer run
        run_a2 = await queue_review(
            session,
            mock_redis,
            full_name="org/swing-repo",
            number=1,
            pr_payload={
                "number": 1,
                "html_url": "https://github.com/org/swing-repo/pull/1",
                "title": "Test PR",
                "user": {"login": "author"},
                "state": "open",
                "head": {"sha": "sha456", "ref": "feature"},
                "base": {"sha": "base456"},
            },
            head_sha="sha456",
            base_sha="base456",
            is_fork=False,
            owner_id="owner-a",
        )

        # A should get a NEW run, not reuse the old completed run_a1
        assert run_a2.id != run_a1.id
        assert run_a2.owner_id == "owner-a"
        assert run_a2.status == "pending"

    @pytest.mark.asyncio
    async def test_findings_isolated_by_owner(self, setup_and_teardown):
        """Findings should be isolated by owner_id."""
        from app.models import Finding, PullRequest, Repository, ReviewRun

        session = setup_and_teardown

        # Create repository and PR
        repo = Repository(full_name="org/finding-repo", enabled=True, owner_id="owner-a")
        session.add(repo)
        await session.flush()

        pr = PullRequest(
            repository_id=repo.id,
            number=1,
            html_url="",
            title="Test",
            author="test",
            state="open",
            base_sha="base",
            head_sha="head",
            head_ref="feature",
        )
        session.add(pr)
        await session.flush()

        # Create runs for both owners
        run_a = ReviewRun(
            pull_request_id=pr.id,
            trigger="webhook",
            base_sha="base",
            head_sha="head",
            status="completed",
            owner_id="owner-a",
        )
        run_b = ReviewRun(
            pull_request_id=pr.id,
            trigger="webhook",
            base_sha="base",
            head_sha="head",
            status="completed",
            owner_id="owner-b",
        )
        session.add_all([run_a, run_b])
        await session.flush()

        # Create findings with SAME stable_id but different owners
        finding_a = Finding(
            pull_request_id=pr.id,
            first_seen_run_id=run_a.id,
            last_seen_run_id=run_a.id,
            owner_id="owner-a",
            stable_id="same-stable-id",
            severity="P2",
            category="security",
            path="src/app.py",
            title="Finding A title",
            scenario="Scenario A",
            evidence="Evidence A",
            recommendation="Rec A",
            current_status="open",
        )
        finding_b = Finding(
            pull_request_id=pr.id,
            first_seen_run_id=run_b.id,
            last_seen_run_id=run_b.id,
            owner_id="owner-b",
            stable_id="same-stable-id",
            severity="P1",
            category="logic",
            path="src/app.py",
            title="Finding B title",
            scenario="Scenario B",
            evidence="Evidence B",
            recommendation="Rec B",
            current_status="open",
        )
        session.add_all([finding_a, finding_b])
        await session.commit()

        # Query findings and verify isolation
        from sqlalchemy import select

        result = await session.execute(
            select(Finding).where(Finding.pull_request_id == pr.id, Finding.owner_id == "owner-a")
        )
        owner_a_findings = list(result.scalars())
        assert len(owner_a_findings) == 1
        assert owner_a_findings[0].title == "Finding A title"
        assert owner_a_findings[0].severity == "P2"

        result = await session.execute(
            select(Finding).where(Finding.pull_request_id == pr.id, Finding.owner_id == "owner-b")
        )
        owner_b_findings = list(result.scalars())
        assert len(owner_b_findings) == 1
        assert owner_b_findings[0].title == "Finding B title"
        assert owner_b_findings[0].severity == "P1"

    @pytest.mark.asyncio
    async def test_legacy_default_findings_mapped_to_new_default_owner(self, setup_and_teardown):
        """Legacy findings with owner_id='default' should map to registry's default owner."""
        from app.models import Finding, PullRequest, Repository, ReviewRun
        from app.services.orchestrator import _filter_findings_for_owner

        session = setup_and_teardown

        # Create repository and PR
        repo = Repository(full_name="org/legacy-repo", enabled=True, owner_id="default")
        session.add(repo)
        await session.flush()

        pr = PullRequest(
            repository_id=repo.id,
            number=1,
            html_url="",
            title="Test",
            author="test",
            state="open",
            base_sha="base",
            head_sha="head",
            head_ref="feature",
        )
        session.add(pr)
        await session.flush()

        # Create legacy run with owner_id='default'
        run = ReviewRun(
            pull_request_id=pr.id,
            trigger="webhook",
            base_sha="base",
            head_sha="head",
            status="completed",
            owner_id="default",
        )
        session.add(run)
        await session.flush()

        # Create legacy finding with owner_id='default'
        finding = Finding(
            pull_request_id=pr.id,
            first_seen_run_id=run.id,
            last_seen_run_id=run.id,
            owner_id="default",
            stable_id="legacy-finding",
            severity="P1",
            category="security",
            path="src/legacy.py",
            title="Legacy finding",
            scenario="Legacy scenario",
            evidence="Legacy evidence",
            recommendation="Legacy rec",
            current_status="open",
        )
        session.add(finding)
        await session.commit()

        # When filtering for 'owner-a' with default_owner_id='owner-a',
        # the legacy 'default' finding should be included
        findings = [finding]
        filtered = _filter_findings_for_owner(findings, "owner-a", default_owner_id="owner-a")
        assert len(filtered) == 1
        assert filtered[0].stable_id == "legacy-finding"

        # When filtering for 'owner-b', the legacy finding should NOT be included
        filtered = _filter_findings_for_owner(findings, "owner-b", default_owner_id="owner-a")
        assert len(filtered) == 0

    async def _queue(self, owner_id: str, *, full_name: str, number: int):
        from app.services.review_enqueue import queue_review

        redis = MagicMock()
        redis.enqueue_job = AsyncMock(return_value=MagicMock(job_id=f"job-{owner_id}"))
        async with self.session_factory() as session:
            run = await queue_review(
                session,
                redis,
                full_name=full_name,
                number=number,
                pr_payload={
                    "number": number,
                    "html_url": f"https://github.com/{full_name}/pull/{number}",
                    "title": "Test PR",
                    "user": {"login": "author"},
                    "state": "open",
                    "head": {"sha": HEAD_SHA, "ref": "feature"},
                    "base": {"sha": BASE_SHA},
                },
                head_sha=HEAD_SHA,
                base_sha=BASE_SHA,
                is_fork=False,
                owner_id=owner_id,
            )
            return run.id

    async def _review(self, run_id, *, current_owner, full_name, number, prompts, stable_id, tag, tmp_path):
        from unittest.mock import patch

        from app.config import AppConfig, Settings
        from app.services import orchestrator
        from app.services.orchestrator import run_review

        checkout = tmp_path / str(run_id)
        checkout.mkdir()

        async def fake_clone(**_kwargs):
            return checkout

        jira = MagicMock()
        jira.enabled.return_value = False
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value={})
        github = _mock_github(full_name, number)
        async with self.session_factory() as session:
            with (
                patch.object(orchestrator, "clone_head", fake_clone),
                patch.object(orchestrator, "cleanup_checkout", lambda _path: None),
            ):
                run = await run_review(
                    session,
                    run_id,
                    settings=Settings.model_construct(),
                    config=AppConfig(),
                    github=github,
                    jira=jira,
                    publisher=publisher,
                    codex_fn=_recording_codex(prompts, stable_id=stable_id, tag=tag),
                    registry=_mock_registry(current_owner),
                )
            return run, github

    @pytest.mark.asyncio
    async def test_run_review_transfer_a_to_b_and_back_isolates_findings(self, setup_and_teardown, tmp_path):
        """End to end: A reviews, repo moves to B (same SHA, same stable_id), then back to A."""
        from app.models import Finding
        from sqlalchemy import select

        full_name, number = "org/transfer-repo", 1
        current_owner = {"id": "owner-a"}
        a_prompts: list[str] = []
        b_prompts: list[str] = []

        run_a1 = await self._queue("owner-a", full_name=full_name, number=number)
        run, _ = await self._review(
            run_a1,
            current_owner=current_owner,
            full_name=full_name,
            number=number,
            prompts=a_prompts,
            stable_id="shared-id",
            tag="OWNER-A-SECRET",
            tmp_path=tmp_path,
        )
        assert run.status == "completed"

        current_owner["id"] = "owner-b"
        run_b = await self._queue("owner-b", full_name=full_name, number=number)
        assert run_b != run_a1
        run, github_b = await self._review(
            run_b,
            current_owner=current_owner,
            full_name=full_name,
            number=number,
            prompts=b_prompts,
            stable_id="shared-id",
            tag="OWNER-B",
            tmp_path=tmp_path,
        )
        assert run.status == "completed", run.error_code
        assert len(b_prompts) == 1
        assert "OWNER-A-SECRET" not in b_prompts[0]
        assert f"previous_head_sha: {HEAD_SHA}" not in b_prompts[0]
        assert "previous_head_sha: null" in b_prompts[0]
        sticky_b = repr(github_b.upsert_sticky_comment.await_args_list)
        assert "OWNER-A-SECRET" not in sticky_b

        current_owner["id"] = "owner-a"
        run_a2 = await self._queue("owner-a", full_name=full_name, number=number)
        assert run_a2 not in {run_a1, run_b}
        run, _ = await self._review(
            run_a2,
            current_owner=current_owner,
            full_name=full_name,
            number=number,
            prompts=a_prompts,
            stable_id="shared-id",
            tag="OWNER-A-AGAIN",
            tmp_path=tmp_path,
        )
        assert run.status == "completed", run.error_code
        assert len(a_prompts) == 2
        assert "OWNER-A-SECRET title" in a_prompts[1]
        assert "OWNER-B" not in a_prompts[1]
        assert f"previous_head_sha: {HEAD_SHA}" in a_prompts[1]

        async with self.session_factory() as session:
            rows = (await session.execute(select(Finding.owner_id, Finding.title))).all()
        assert sorted(rows) == [("owner-a", "OWNER-A-AGAIN title"), ("owner-b", "OWNER-B title")]

    async def _seed_pr_with_runs(self, *, repo_owner: str, runs: list[tuple[str, str]]):
        """Create a repo/PR and completed runs given as (owner_id, head_sha), oldest first."""
        from datetime import UTC, datetime, timedelta

        from app.models import PullRequest, Repository, ReviewRun

        async with self.session_factory() as session:
            repo = Repository(full_name=f"org/{uuid.uuid4().hex[:8]}", enabled=True, owner_id=repo_owner)
            session.add(repo)
            await session.flush()
            pr = PullRequest(repository_id=repo.id, number=1, state="open", base_sha=BASE_SHA, head_sha=HEAD_SHA)
            session.add(pr)
            await session.flush()
            start = datetime.now(UTC) - timedelta(hours=len(runs))
            run_ids = []
            for index, (owner_id, sha) in enumerate(runs):
                run = ReviewRun(
                    pull_request_id=pr.id,
                    trigger="webhook",
                    base_sha=BASE_SHA,
                    head_sha=sha,
                    status="completed",
                    owner_id=owner_id,
                    created_at=start + timedelta(hours=index),
                )
                session.add(run)
                await session.flush()
                run_ids.append(run.id)
            await session.commit()
            return pr.id, run_ids

    @pytest.mark.asyncio
    async def test_previous_head_filters_by_owner(self, setup_and_teardown):
        from app.services.orchestrator import _previous_head

        pr_id, _ = await self._seed_pr_with_runs(
            repo_owner="owner-b", runs=[("owner-a", "a" * 40), ("owner-b", "d" * 40)]
        )
        async with self.session_factory() as session:
            current = uuid.uuid4()
            assert await _previous_head(session, pr_id, current, "owner-a", "owner-a") == "a" * 40
            assert await _previous_head(session, pr_id, current, "owner-b", "owner-a") == "d" * 40
            assert await _previous_head(session, pr_id, current, "owner-c", "owner-a") is None

    @pytest.mark.asyncio
    async def test_previous_head_maps_legacy_default_runs_to_default_owner(self, setup_and_teardown):
        from app.services.orchestrator import _previous_head

        pr_id, _ = await self._seed_pr_with_runs(repo_owner="owner-a", runs=[("default", "e" * 40)])
        async with self.session_factory() as session:
            current = uuid.uuid4()
            assert await _previous_head(session, pr_id, current, "owner-a", "owner-a") == "e" * 40
            assert await _previous_head(session, pr_id, current, "owner-b", "owner-a") is None
            assert await _previous_head(session, pr_id, current, "owner-a", None) is None

    @pytest.mark.asyncio
    async def test_digest_blockers_count_only_owner_findings(self, setup_and_teardown):
        from app.models import Finding, PullRequest, Repository
        from app.services.digest import build_digest_payload

        pr_b, runs_b = await self._seed_pr_with_runs(repo_owner="owner-b", runs=[("owner-a", HEAD_SHA)])
        pr_a, runs_a = await self._seed_pr_with_runs(repo_owner="owner-a", runs=[("default", HEAD_SHA)])
        async with self.session_factory() as session:
            for pr_id, run_id, owner_id in ((pr_b, runs_b[0], "owner-a"), (pr_a, runs_a[0], "default")):
                session.add(
                    Finding(
                        pull_request_id=pr_id,
                        first_seen_run_id=run_id,
                        last_seen_run_id=run_id,
                        owner_id=owner_id,
                        stable_id="blocker",
                        severity="P1",
                        category="security",
                        path="src/app.py",
                        title="blocker",
                        current_status="open",
                    )
                )
            await session.commit()
            assert await session.get(PullRequest, pr_a) is not None
            assert await session.get(Repository, (await session.get(PullRequest, pr_b)).repository_id) is not None

            # owner-b's PR only carries owner-a's finding: not a blocker for owner-b.
            payload_b = await build_digest_payload(session, owner_id="owner-b", default_owner_id="owner-a")
            assert (payload_b["open_pr_count"], payload_b["blocker_pr_count"]) == (1, 0)
            # owner-a (registry default) inherits the legacy 'default' finding.
            payload_a = await build_digest_payload(session, owner_id="owner-a", default_owner_id="owner-a")
            assert (payload_a["open_pr_count"], payload_a["blocker_pr_count"]) == (1, 1)

    @pytest.mark.asyncio
    async def test_digest_open_prs_passes_registry_default_owner(self, setup_and_teardown):
        from types import SimpleNamespace
        from unittest.mock import patch

        from app.config import AppConfig
        from app.workers import settings as worker_settings

        registry = MagicMock()
        registry.default_owner_id = "owner-a"
        registry.owners = {"owner-a": SimpleNamespace(github=None, config=AppConfig())}
        registry.get_allowed_repos_for_owner.return_value = ["org/repo"]
        run_digest = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
        with (
            patch.object(worker_settings, "get_app_config", return_value=AppConfig()),
            patch("app.owners.registry.get_owner_registry", return_value=registry),
            patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=MagicMock()),
            patch.object(worker_settings, "run_digest", run_digest),
        ):
            await worker_settings.digest_open_prs({"session_factory": self.session_factory})
        assert run_digest.await_args.kwargs["default_owner_id"] == "owner-a"
