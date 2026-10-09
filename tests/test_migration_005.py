"""Migration 005 tests: owner_id columns.

These tests run against a real Postgres database and are skipped if TEST_DATABASE_URL is not set.
The database name must end with '_test' to prevent accidental runs against production databases.

Note: In M1, upgrade only adds owner_id columns; comment_authors data conversion is deferred to M2.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


def _validate_test_database_url() -> str | None:
    """Validate TEST_DATABASE_URL and return a reason if invalid."""
    if not TEST_DB_URL:
        return "TEST_DATABASE_URL not set"
    # Extract database name from URL (handles both postgresql:// and postgresql+asyncpg://)
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
    # Disable logger configuration to avoid breaking other tests
    config.attributes["configure_logger"] = False
    return config


def _run_migrations(config, target: str) -> None:
    """Run alembic migrations. Called synchronously since alembic handles async internally."""
    import asyncio

    from alembic import command

    # Run in a new event loop since we may be called from within pytest's event loop
    def run_sync():
        command.upgrade(config, target)

    # If there's a running loop, we need to run in a thread
    try:
        asyncio.get_running_loop()
        # Running inside an event loop - use thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(run_sync).result()
    except RuntimeError:
        # No running loop - call directly
        run_sync()


def _downgrade_migrations(config, target: str) -> None:
    """Run alembic downgrade. Called synchronously since alembic handles async internally."""
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
class TestMigration005:
    """Test migration 005: owner_id columns."""

    @pytest.fixture(autouse=True)
    async def setup_and_teardown(self):
        """Set up test database at revision 004, run tests, then clean up."""
        import asyncpg

        config = _get_alembic_config()

        # Downgrade to base and upgrade to 004 (sync calls, handle event loop internally)
        _downgrade_migrations(config, "base")
        _run_migrations(config, "004_finding_details_visibility")

        # Parse connection params from URL
        url = _get_asyncpg_url()
        # Convert asyncpg URL to connection params
        dsn = url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)

        yield conn

        await conn.close()
        _downgrade_migrations(config, "base")

    @pytest.mark.asyncio
    async def test_upgrade_adds_owner_id_columns_and_indexes(self, setup_and_teardown):
        """Upgrade should add owner_id columns with default='default' and indexes."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "005_owner_id")

        # Check repositories.owner_id
        row = await conn.fetchrow("""
            SELECT column_name, column_default
            FROM information_schema.columns
            WHERE table_name = 'repositories' AND column_name = 'owner_id'
        """)
        assert row is not None
        assert row["column_name"] == "owner_id"
        assert "'default'" in row["column_default"]

        # Check review_runs.owner_id
        row = await conn.fetchrow("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'review_runs' AND column_name = 'owner_id'
        """)
        assert row is not None

        # Check digest_runs.owner_id
        row = await conn.fetchrow("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'digest_runs' AND column_name = 'owner_id'
        """)
        assert row is not None

        # Check indexes
        row = await conn.fetchrow("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'repositories' AND indexname = 'ix_repositories_owner_id'
        """)
        assert row is not None

        row = await conn.fetchrow("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'review_runs' AND indexname = 'ix_review_runs_owner_id'
        """)
        assert row is not None

    @pytest.mark.asyncio
    async def test_upgrade_preserves_comment_authors_as_strings(self, setup_and_teardown):
        """Upgrade should NOT convert comment_authors in M1 (preserves plain strings)."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()
        old_authors = ["user1", "bot[bot]", "user2"]

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
            VALUES ($1, $2, true, 'default', $3::jsonb)
        """,
            repo_id,
            "org/repo-strings",
            json.dumps(old_authors),
        )

        _run_migrations(config, "005_owner_id")

        row = await conn.fetchrow("SELECT comment_authors, owner_id FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])
        owner_id = row["owner_id"]

        # owner_id should be 'default'
        assert owner_id == "default"
        # comment_authors should be unchanged (still plain strings in M1)
        assert authors == old_authors

    @pytest.mark.asyncio
    async def test_upgrade_handles_empty_list(self, setup_and_teardown):
        """Upgrade should handle empty comment_authors list."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
            VALUES ($1, $2, true, 'default', $3::jsonb)
        """,
            repo_id,
            "org/repo-empty",
            json.dumps([]),
        )

        _run_migrations(config, "005_owner_id")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        assert json.loads(row["comment_authors"]) == []

    @pytest.mark.asyncio
    async def test_downgrade_removes_owner_id_columns(self, setup_and_teardown):
        """Downgrade should remove owner_id columns and indexes."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
            VALUES ($1, $2, true, 'default', $3::jsonb)
        """,
            repo_id,
            "org/repo-downgrade",
            json.dumps(["user1", "user2"]),
        )

        _run_migrations(config, "005_owner_id")

        # Verify owner_id exists
        row = await conn.fetchrow("SELECT owner_id FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        assert row["owner_id"] == "default"

        _downgrade_migrations(config, "004_finding_details_visibility")

        # Verify owner_id column is removed
        row = await conn.fetchrow("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'repositories' AND column_name = 'owner_id'
        """)
        assert row is None

        # Verify comment_authors is unchanged (still strings)
        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        assert json.loads(row["comment_authors"]) == ["user1", "user2"]

    @pytest.mark.asyncio
    async def test_downgrade_converts_object_authors_to_strings(self, setup_and_teardown):
        """Downgrade should convert object-format comment_authors to plain strings (safety net)."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "005_owner_id")

        repo_id = uuid.uuid4()
        object_authors = [
            {"login": "object-user", "owner_id": "custom", "kind": "app"},
            "string-user",
            {"login": "another-object", "owner_id": "default", "kind": None},
        ]

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', $3::jsonb, 'default')
        """,
            repo_id,
            "org/repo-objects",
            json.dumps(object_authors),
        )

        _downgrade_migrations(config, "004_finding_details_visibility")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])

        # Should be converted to plain strings
        assert authors == ["object-user", "string-user", "another-object"]
