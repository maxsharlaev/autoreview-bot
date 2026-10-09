"""Migration 007 tests: findings owner_id column.

These tests run against a real Postgres database and are skipped if TEST_DATABASE_URL is not set.
The database name must end with '_test' to prevent accidental runs against production databases.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest

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
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(run_sync).result()
    except RuntimeError:
        run_sync()


def _downgrade_migrations(config, target: str) -> None:
    """Run alembic downgrade."""
    import asyncio

    from alembic import command

    def run_sync():
        command.downgrade(config, target)

    try:
        asyncio.get_running_loop()
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(run_sync).result()
    except RuntimeError:
        run_sync()


@requires_test_postgres
class TestMigration007:
    """Test migration 007: findings owner_id column."""

    @pytest.fixture(autouse=True)
    async def setup_and_teardown(self):
        """Set up test database at revision 006, run tests, then clean up."""
        import asyncpg

        config = _get_alembic_config()

        _downgrade_migrations(config, "base")
        _run_migrations(config, "006_comment_authors_objects")

        url = _get_asyncpg_url()
        dsn = url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)

        yield conn

        await conn.close()
        _downgrade_migrations(config, "base")

    @pytest.mark.asyncio
    async def test_upgrade_adds_owner_id_column(self, setup_and_teardown):
        """Upgrade should add owner_id column to findings table."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        # Check column does not exist before migration
        columns = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'findings' AND column_name = 'owner_id'
            """
        )
        assert len(columns) == 0

        _run_migrations(config, "007_findings_owner_id")

        # Check column exists after migration
        columns = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'findings' AND column_name = 'owner_id'
            """
        )
        assert len(columns) == 1

    @pytest.mark.asyncio
    async def test_upgrade_backfills_owner_id_from_run(self, setup_and_teardown):
        """Upgrade should backfill owner_id from first_seen_run's owner_id."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        # Create test data
        repo_id = uuid.uuid4()
        pr_id = uuid.uuid4()
        run_id = uuid.uuid4()
        finding_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, 'org/repo', true, 'default', '[]'::jsonb, 'org-a')
            """,
            repo_id,
        )
        await conn.execute(
            """
            INSERT INTO pull_requests (
                id, repository_id, number, html_url, title, author, state,
                base_sha, head_sha, head_ref, is_draft, is_fork
            ) VALUES ($1, $2, 1, '', '', '', 'open', 'base', 'head', 'main', false, false)
            """,
            pr_id,
            repo_id,
        )
        await conn.execute(
            """
            INSERT INTO review_runs (
                id, pull_request_id, trigger, base_sha, head_sha,
                status, prompt_version, policy_version, owner_id
            ) VALUES ($1, $2, 'webhook', 'base', 'head', 'completed', 'v1', 'v1', 'org-a')
            """,
            run_id,
            pr_id,
        )
        await conn.execute(
            """
            INSERT INTO findings (
                id, pull_request_id, first_seen_run_id, last_seen_run_id, stable_id,
                severity, category, path, title, scenario, evidence, recommendation,
                current_status
            ) VALUES ($1, $2, $3, $3, 'stable-1', 'P2', 'security', 'src/app.py',
                'Test finding', '', '', '', 'open')
            """,
            finding_id,
            pr_id,
            run_id,
        )

        _run_migrations(config, "007_findings_owner_id")

        # Check owner_id was backfilled
        row = await conn.fetchrow("SELECT owner_id FROM findings WHERE id = $1", finding_id)
        assert row is not None
        assert row["owner_id"] == "org-a"

    @pytest.mark.asyncio
    async def test_upgrade_creates_new_unique_constraint(self, setup_and_teardown):
        """Upgrade should create new unique constraint on (pull_request_id, owner_id, stable_id)."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "007_findings_owner_id")

        # Check new constraint exists
        constraints = await conn.fetch(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'findings' AND constraint_type = 'UNIQUE'
            """
        )
        constraint_names = [c["constraint_name"] for c in constraints]
        assert "uq_findings_pr_owner_stable" in constraint_names
        assert "uq_findings_pr_stable" not in constraint_names

    @pytest.mark.asyncio
    async def test_upgrade_allows_same_stable_id_different_owners(self, setup_and_teardown):
        """After upgrade, same stable_id with different owners should not conflict."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "007_findings_owner_id")

        # Create test data
        repo_id = uuid.uuid4()
        pr_id = uuid.uuid4()
        run_a_id = uuid.uuid4()
        run_b_id = uuid.uuid4()
        finding_a_id = uuid.uuid4()
        finding_b_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (
                id, full_name, enabled, policy_profile, comment_authors, owner_id
            ) VALUES ($1, 'org/shared-repo', true, 'default', '[]'::jsonb, 'org-a')
            """,
            repo_id,
        )
        await conn.execute(
            """
            INSERT INTO pull_requests (
                id, repository_id, number, html_url, title, author, state,
                base_sha, head_sha, head_ref, is_draft, is_fork
            ) VALUES ($1, $2, 1, '', '', '', 'open', 'base', 'head', 'main', false, false)
            """,
            pr_id,
            repo_id,
        )
        await conn.execute(
            """
            INSERT INTO review_runs (
                id, pull_request_id, trigger, base_sha, head_sha,
                status, prompt_version, policy_version, owner_id
            ) VALUES ($1, $2, 'webhook', 'base', 'head', 'completed', 'v1', 'v1', 'org-a')
            """,
            run_a_id,
            pr_id,
        )
        await conn.execute(
            """
            INSERT INTO review_runs (
                id, pull_request_id, trigger, base_sha, head_sha,
                status, prompt_version, policy_version, owner_id
            ) VALUES ($1, $2, 'webhook', 'base', 'head', 'completed', 'v1', 'v1', 'org-b')
            """,
            run_b_id,
            pr_id,
        )

        # Insert finding for owner A
        await conn.execute(
            """
            INSERT INTO findings (
                id, pull_request_id, first_seen_run_id, last_seen_run_id, owner_id,
                stable_id, severity, category, path, title, scenario,
                evidence, recommendation, current_status
            ) VALUES ($1, $2, $3, $3, 'org-a', 'same-stable-id', 'P2', 'security',
                'src/app.py', 'Finding A', '', '', '', 'open')
            """,
            finding_a_id,
            pr_id,
            run_a_id,
        )

        # Insert finding for owner B with SAME stable_id - should NOT conflict
        await conn.execute(
            """
            INSERT INTO findings (
                id, pull_request_id, first_seen_run_id, last_seen_run_id, owner_id,
                stable_id, severity, category, path, title, scenario,
                evidence, recommendation, current_status
            ) VALUES ($1, $2, $3, $3, 'org-b', 'same-stable-id', 'P2', 'security',
                'src/app.py', 'Finding B', '', '', '', 'open')
            """,
            finding_b_id,
            pr_id,
            run_b_id,
        )

        # Verify both exist
        findings = await conn.fetch("SELECT owner_id, title FROM findings WHERE stable_id = 'same-stable-id'")
        assert len(findings) == 2
        owner_ids = {f["owner_id"] for f in findings}
        assert owner_ids == {"org-a", "org-b"}

    @pytest.mark.asyncio
    async def test_downgrade_removes_owner_id_column(self, setup_and_teardown):
        """Downgrade should remove owner_id column from findings."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "007_findings_owner_id")

        # Verify column exists
        columns = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'findings' AND column_name = 'owner_id'
            """
        )
        assert len(columns) == 1

        _downgrade_migrations(config, "006_comment_authors_objects")

        # Verify column removed
        columns = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'findings' AND column_name = 'owner_id'
            """
        )
        assert len(columns) == 0

    @pytest.mark.asyncio
    async def test_downgrade_restores_old_unique_constraint(self, setup_and_teardown):
        """Downgrade should restore old unique constraint on (pull_request_id, stable_id)."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "007_findings_owner_id")
        _downgrade_migrations(config, "006_comment_authors_objects")

        # Check old constraint restored
        constraints = await conn.fetch(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'findings' AND constraint_type = 'UNIQUE'
            """
        )
        constraint_names = [c["constraint_name"] for c in constraints]
        assert "uq_findings_pr_stable" in constraint_names
        assert "uq_findings_pr_owner_stable" not in constraint_names

    @pytest.mark.asyncio
    async def test_downgrade_handles_duplicate_stable_ids(self, setup_and_teardown):
        """Downgrade should delete newer duplicate findings to satisfy old constraint."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "007_findings_owner_id")

        # Create test data with same stable_id for different owners
        repo_id = uuid.uuid4()
        pr_id = uuid.uuid4()
        run_a_id = uuid.uuid4()
        run_b_id = uuid.uuid4()
        finding_a_id = uuid.uuid4()
        finding_b_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (
                id, full_name, enabled, policy_profile, comment_authors, owner_id
            ) VALUES ($1, 'org/downgrade-repo', true, 'default', '[]'::jsonb, 'org-a')
            """,
            repo_id,
        )
        await conn.execute(
            """
            INSERT INTO pull_requests (
                id, repository_id, number, html_url, title, author, state,
                base_sha, head_sha, head_ref, is_draft, is_fork
            ) VALUES ($1, $2, 1, '', '', '', 'open', 'base', 'head', 'main', false, false)
            """,
            pr_id,
            repo_id,
        )
        await conn.execute(
            """
            INSERT INTO review_runs (
                id, pull_request_id, trigger, base_sha, head_sha,
                status, prompt_version, policy_version, owner_id
            ) VALUES ($1, $2, 'webhook', 'base', 'head', 'completed', 'v1', 'v1', 'org-a')
            """,
            run_a_id,
            pr_id,
        )
        await conn.execute(
            """
            INSERT INTO review_runs (
                id, pull_request_id, trigger, base_sha, head_sha,
                status, prompt_version, policy_version, owner_id
            ) VALUES ($1, $2, 'webhook', 'base', 'head', 'completed', 'v1', 'v1', 'org-b')
            """,
            run_b_id,
            pr_id,
        )

        # Insert older finding for owner A
        await conn.execute(
            """
            INSERT INTO findings (
                id, pull_request_id, first_seen_run_id, last_seen_run_id, owner_id,
                stable_id, severity, category, path, title, scenario,
                evidence, recommendation, current_status, created_at
            ) VALUES ($1, $2, $3, $3, 'org-a', 'conflict-stable-id', 'P2', 'security',
                'src/app.py', 'Finding A', '', '', '', 'open', NOW() - INTERVAL '1 hour')
            """,
            finding_a_id,
            pr_id,
            run_a_id,
        )

        # Insert newer finding for owner B with same stable_id
        await conn.execute(
            """
            INSERT INTO findings (
                id, pull_request_id, first_seen_run_id, last_seen_run_id, owner_id,
                stable_id, severity, category, path, title, scenario,
                evidence, recommendation, current_status, created_at
            ) VALUES ($1, $2, $3, $3, 'org-b', 'conflict-stable-id', 'P2', 'security',
                'src/app.py', 'Finding B', '', '', '', 'open', NOW())
            """,
            finding_b_id,
            pr_id,
            run_b_id,
        )

        # Verify both exist before downgrade
        findings = await conn.fetch("SELECT id FROM findings WHERE stable_id = 'conflict-stable-id'")
        assert len(findings) == 2

        # Downgrade should delete the newer one to avoid conflict
        _downgrade_migrations(config, "006_comment_authors_objects")

        # Only older finding should remain
        findings = await conn.fetch("SELECT id FROM findings WHERE stable_id = 'conflict-stable-id'")
        assert len(findings) == 1
        assert findings[0]["id"] == finding_a_id

    @pytest.mark.asyncio
    async def test_legacy_default_findings_kept_on_upgrade(self, setup_and_teardown):
        """Legacy findings with owner_id='default' should be kept and queryable."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        # Create legacy finding (before migration, owner_id comes from run)
        repo_id = uuid.uuid4()
        pr_id = uuid.uuid4()
        run_id = uuid.uuid4()
        finding_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (
                id, full_name, enabled, policy_profile, comment_authors, owner_id
            ) VALUES ($1, 'org/legacy-repo', true, 'default', '[]'::jsonb, 'default')
            """,
            repo_id,
        )
        await conn.execute(
            """
            INSERT INTO pull_requests (
                id, repository_id, number, html_url, title, author, state,
                base_sha, head_sha, head_ref, is_draft, is_fork
            ) VALUES ($1, $2, 1, '', '', '', 'open', 'base', 'head', 'main', false, false)
            """,
            pr_id,
            repo_id,
        )
        # Legacy run with owner_id='default'
        await conn.execute(
            """
            INSERT INTO review_runs (
                id, pull_request_id, trigger, base_sha, head_sha,
                status, prompt_version, policy_version, owner_id
            ) VALUES ($1, $2, 'webhook', 'base', 'head', 'completed', 'v1', 'v1', 'default')
            """,
            run_id,
            pr_id,
        )
        await conn.execute(
            """
            INSERT INTO findings (
                id, pull_request_id, first_seen_run_id, last_seen_run_id, stable_id,
                severity, category, path, title, scenario, evidence, recommendation,
                current_status
            ) VALUES ($1, $2, $3, $3, 'legacy-stable', 'P1', 'security',
                'src/legacy.py', 'Legacy finding', '', '', '', 'open')
            """,
            finding_id,
            pr_id,
            run_id,
        )

        _run_migrations(config, "007_findings_owner_id")

        # Legacy finding should have owner_id='default' (not backfilled since run was 'default')
        row = await conn.fetchrow("SELECT owner_id FROM findings WHERE id = $1", finding_id)
        assert row is not None
        assert row["owner_id"] == "default"
