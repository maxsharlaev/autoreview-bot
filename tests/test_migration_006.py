"""Migration 006 tests: comment_authors object conversion.

These tests run against a real Postgres database and are skipped if TEST_DATABASE_URL is not set.
The database name must end with '_test' to prevent accidental runs against production databases.
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
class TestMigration006:
    """Test migration 006: comment_authors object conversion."""

    @pytest.fixture(autouse=True)
    async def setup_and_teardown(self):
        """Set up test database at revision 005, run tests, then clean up."""
        import asyncpg

        config = _get_alembic_config()

        _downgrade_migrations(config, "base")
        _run_migrations(config, "005_owner_id")

        url = _get_asyncpg_url()
        dsn = url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)

        yield conn

        await conn.close()
        _downgrade_migrations(config, "base")

    @pytest.mark.asyncio
    async def test_upgrade_converts_string_authors_to_objects(self, setup_and_teardown):
        """Upgrade should convert plain string authors to object format."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()
        string_authors = ["user1", "bot[bot]", "user2"]

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', $3::jsonb, 'default')
        """,
            repo_id,
            "org/repo-strings",
            json.dumps(string_authors),
        )

        _run_migrations(config, "006_comment_authors_objects")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])

        assert len(authors) == 3
        assert authors[0] == {"login": "user1", "owner_id": "default", "kind": None}
        assert authors[1] == {"login": "bot[bot]", "owner_id": "default", "kind": None}
        assert authors[2] == {"login": "user2", "owner_id": "default", "kind": None}

    @pytest.mark.asyncio
    async def test_upgrade_uses_repo_owner_id(self, setup_and_teardown):
        """Upgrade should use the repository's owner_id for converted authors."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()
        string_authors = ["user1", "user2"]

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', $3::jsonb, 'custom-owner')
        """,
            repo_id,
            "org/repo-custom-owner",
            json.dumps(string_authors),
        )

        _run_migrations(config, "006_comment_authors_objects")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])

        # Should use the repo's owner_id
        assert authors[0]["owner_id"] == "custom-owner"
        assert authors[1]["owner_id"] == "custom-owner"

    @pytest.mark.asyncio
    async def test_upgrade_preserves_existing_objects(self, setup_and_teardown):
        """Upgrade should preserve already-converted object authors."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()
        mixed_authors = [
            {"login": "existing-user", "owner_id": "org-a", "kind": "app"},
            "new-string-user",
        ]

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', $3::jsonb, 'default')
        """,
            repo_id,
            "org/repo-mixed",
            json.dumps(mixed_authors),
        )

        _run_migrations(config, "006_comment_authors_objects")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])

        assert len(authors) == 2
        # Existing object preserved
        assert authors[0] == {"login": "existing-user", "owner_id": "org-a", "kind": "app"}
        # String converted
        assert authors[1] == {"login": "new-string-user", "owner_id": "default", "kind": None}

    @pytest.mark.asyncio
    async def test_upgrade_handles_empty_list(self, setup_and_teardown):
        """Upgrade should handle empty comment_authors list."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', $3::jsonb, 'default')
        """,
            repo_id,
            "org/repo-empty",
            json.dumps([]),
        )

        _run_migrations(config, "006_comment_authors_objects")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        assert json.loads(row["comment_authors"]) == []

    @pytest.mark.asyncio
    async def test_downgrade_converts_objects_to_strings(self, setup_and_teardown):
        """Downgrade should convert object authors back to plain strings."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "006_comment_authors_objects")

        repo_id = uuid.uuid4()
        object_authors = [
            {"login": "user1", "owner_id": "org-a", "kind": "app"},
            {"login": "user2", "owner_id": "org-b", "kind": None},
        ]

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', $3::jsonb, 'default')
        """,
            repo_id,
            "org/repo-downgrade",
            json.dumps(object_authors),
        )

        _downgrade_migrations(config, "005_owner_id")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])

        # Should be converted to plain strings (just logins)
        assert authors == ["user1", "user2"]

    @pytest.mark.asyncio
    async def test_downgrade_preserves_string_authors(self, setup_and_teardown):
        """Downgrade should preserve already-string authors."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        _run_migrations(config, "006_comment_authors_objects")

        repo_id = uuid.uuid4()
        mixed_authors = [
            {"login": "object-user", "owner_id": "org-a", "kind": None},
            "string-user",
        ]

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', $3::jsonb, 'default')
        """,
            repo_id,
            "org/repo-mixed-downgrade",
            json.dumps(mixed_authors),
        )

        _downgrade_migrations(config, "005_owner_id")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])

        # Object converted, string preserved
        assert authors == ["object-user", "string-user"]

    @pytest.mark.asyncio
    async def test_upgrade_handles_null_comment_authors(self, setup_and_teardown):
        """Upgrade should handle NULL comment_authors gracefully."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
            VALUES ($1, $2, true, 'default', NULL, 'default')
        """,
            repo_id,
            "org/repo-null",
        )

        # Should not raise
        _run_migrations(config, "006_comment_authors_objects")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        assert row["comment_authors"] is None

    @pytest.mark.asyncio
    async def test_upgrade_uses_default_owner_id_via_server_default(self, setup_and_teardown):
        """Upgrade should use 'default' owner_id from server default when inserted without explicit value."""
        conn = setup_and_teardown
        config = _get_alembic_config()

        repo_id = uuid.uuid4()

        await conn.execute(
            """
            INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
            VALUES ($1, $2, true, 'default', $3::jsonb)
        """,
            repo_id,
            "org/repo-server-default",
            json.dumps(["user1"]),
        )

        _run_migrations(config, "006_comment_authors_objects")

        row = await conn.fetchrow("SELECT comment_authors FROM repositories WHERE id = $1", repo_id)
        assert row is not None
        authors = json.loads(row["comment_authors"])

        assert authors[0]["owner_id"] == "default"
