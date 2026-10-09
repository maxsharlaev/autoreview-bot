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


def _fake_codex_fn(finding_id: str, finding_title: str, sha: str):
    """Create a fake codex_fn that returns a finding."""
    import json

    async def fake_codex(
        *,
        checkout,
        prompt,
        schema_path,
        output_path,
        model,
        timeout_seconds,
        openai_api_key,
        reasoning_effort,
        sandbox,
        approval_policy,
    ):
        result = {
            "summary": "Test review summary",
            "task_alignment": {"status": "unclear"},
            "findings": [
                {
                    "id": finding_id,
                    "severity": "P2",
                    "category": "security",
                    "path": "src/app.py",
                    "line": 10,
                    "title": finding_title,
                    "scenario": "Test scenario",
                    "evidence": "Test evidence",
                    "recommendation": "Test recommendation",
                }
            ],
            "previous_findings": [],
            "head_sha": sha,
        }
        output_path.write_text(json.dumps(result))
        return json.dumps(result)

    return fake_codex


def _make_mock_github(owner: str, repo: str, pr_number: int, sha: str, visibility: str = "private"):
    """Create a mock GitHub client."""
    from types import SimpleNamespace

    from app.adapters.github import PullRequestInfo

    info = PullRequestInfo(
        owner=owner,
        repo=repo,
        number=pr_number,
        full_name=f"{owner}/{repo}",
        html_url=f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        title="Test PR",
        body="Test body",
        state="open",
        draft=False,
        author="test-user",
        assignee=None,
        base_sha="base123",
        head_sha=sha,
        base_ref="main",
        head_ref="feature",
        is_fork=False,
        visibility=visibility,
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


def _make_mock_jira():
    """Create a disabled mock Jira client."""
    mock = MagicMock()
    mock.enabled.return_value = False
    return mock


def _make_mock_publisher():
    """Create a mock publisher."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value={})
    return mock


def _make_mock_registry(default_owner_id: str = "owner-a"):
    """Create a mock registry with multiple owners."""
    from types import SimpleNamespace

    from app.config import AppConfig
    from app.owners.registry import ROUTE_EXACT, RouteResult

    owner_a_ctx = SimpleNamespace(
        id="owner-a",
        config=AppConfig(),
        openai_api_key="test-key",
        jira=None,
        slack=None,
        github=SimpleNamespace(kind="pat", token="ghp_test_a"),
    )
    owner_b_ctx = SimpleNamespace(
        id="owner-b",
        config=AppConfig(),
        openai_api_key="test-key",
        jira=None,
        slack=None,
        github=SimpleNamespace(kind="pat", token="ghp_test_b"),
    )

    mock = MagicMock()
    mock.default_owner_id = default_owner_id

    def get_owner(owner_id):
        if owner_id == "owner-a":
            return owner_a_ctx
        if owner_id == "owner-b":
            return owner_b_ctx
        return None

    mock.get.side_effect = get_owner
    mock.is_disabled.return_value = False
    mock.resolve.return_value = RouteResult(default_owner_id, ROUTE_EXACT)
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
        _downgrade_migrations(config, "base")
        _run_migrations(config, "head")

        engine = create_async_engine(_get_asyncpg_url())
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with async_session() as session:
            yield session

        await engine.dispose()
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
