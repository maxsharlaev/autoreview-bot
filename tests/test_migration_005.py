"""Migration 005 tests: owner_id columns and comment_authors data migration.

These tests run against a real Postgres database and are skipped if DATABASE_URL is not set.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

requires_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; skipping migration tests",
)


def get_alembic_config() -> Config:
    """Get Alembic config pointing to the test database."""
    config = Config("alembic.ini")
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def get_sync_engine():
    """Get a synchronous SQLAlchemy engine for testing."""
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    return create_engine(db_url)


@requires_postgres
class TestMigration005:
    """Test migration 005: owner_id columns and comment_authors data migration."""

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up test database at revision 004, run tests, then clean up."""
        config = get_alembic_config()
        engine = get_sync_engine()

        command.downgrade(config, "base")
        command.upgrade(config, "004_finding_details_visibility")

        yield engine

        command.downgrade(config, "base")

    def test_upgrade_adds_owner_id_columns_and_indexes(self, setup_and_teardown):
        """Upgrade should add owner_id columns with default='default' and indexes."""
        engine = setup_and_teardown
        config = get_alembic_config()

        command.upgrade(config, "005_owner_id")

        with engine.connect() as conn:
            result = conn.execute(
                text("""
                SELECT column_name, column_default
                FROM information_schema.columns
                WHERE table_name = 'repositories' AND column_name = 'owner_id'
            """)
            )
            row = result.fetchone()
            assert row is not None
            assert row[0] == "owner_id"
            assert "'default'" in row[1]

            result = conn.execute(
                text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'review_runs' AND column_name = 'owner_id'
            """)
            )
            assert result.fetchone() is not None

            result = conn.execute(
                text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'digest_runs' AND column_name = 'owner_id'
            """)
            )
            assert result.fetchone() is not None

            result = conn.execute(
                text("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'repositories' AND indexname = 'ix_repositories_owner_id'
            """)
            )
            assert result.fetchone() is not None

            result = conn.execute(
                text("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'review_runs' AND indexname = 'ix_review_runs_owner_id'
            """)
            )
            assert result.fetchone() is not None

    def test_upgrade_migrates_comment_authors_strings_to_objects(self, setup_and_teardown):
        """Upgrade should convert string-format comment_authors to object format."""
        engine = setup_and_teardown
        config = get_alembic_config()

        repo_id = uuid.uuid4()
        old_authors = ["user1", "bot[bot]", "user2"]

        with engine.connect() as conn:
            conn.execute(
                text("""
                INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
                VALUES (:id, :name, true, 'default', CAST(:authors AS jsonb))
            """),
                {"id": repo_id, "name": "org/repo-strings", "authors": json.dumps(old_authors)},
            )
            conn.commit()

        command.upgrade(config, "005_owner_id")

        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT comment_authors, owner_id FROM repositories WHERE id = :id"), {"id": repo_id}
            )
            row = result.fetchone()
            assert row is not None
            authors = row[0]
            owner_id = row[1]

            assert owner_id == "default"
            assert isinstance(authors, list)
            assert len(authors) == 3
            for i, author in enumerate(authors):
                assert isinstance(author, dict)
                assert author["login"] == old_authors[i]
                assert author["owner_id"] == "default"
                assert author["kind"] is None

    def test_upgrade_handles_empty_list(self, setup_and_teardown):
        """Upgrade should handle empty comment_authors list."""
        engine = setup_and_teardown
        config = get_alembic_config()

        repo_id = uuid.uuid4()

        with engine.connect() as conn:
            conn.execute(
                text("""
                INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
                VALUES (:id, :name, true, 'default', CAST(:authors AS jsonb))
            """),
                {"id": repo_id, "name": "org/repo-empty", "authors": json.dumps([])},
            )
            conn.commit()

        command.upgrade(config, "005_owner_id")

        with engine.connect() as conn:
            result = conn.execute(text("SELECT comment_authors FROM repositories WHERE id = :id"), {"id": repo_id})
            row = result.fetchone()
            assert row is not None
            assert row[0] == []

    def test_upgrade_handles_mixed_list(self, setup_and_teardown):
        """Upgrade should normalize mixed lists (strings + objects)."""
        engine = setup_and_teardown
        config = get_alembic_config()

        repo_id = uuid.uuid4()
        mixed_authors = [
            "string-user",
            {"login": "object-user", "owner_id": "custom", "kind": "app"},
            "another-string",
        ]

        with engine.connect() as conn:
            conn.execute(
                text("""
                INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
                VALUES (:id, :name, true, 'default', CAST(:authors AS jsonb))
            """),
                {"id": repo_id, "name": "org/repo-mixed", "authors": json.dumps(mixed_authors)},
            )
            conn.commit()

        command.upgrade(config, "005_owner_id")

        with engine.connect() as conn:
            result = conn.execute(text("SELECT comment_authors FROM repositories WHERE id = :id"), {"id": repo_id})
            row = result.fetchone()
            assert row is not None
            authors = row[0]

            assert len(authors) == 3
            assert authors[0] == {"login": "string-user", "owner_id": "default", "kind": None}
            assert authors[1] == {"login": "object-user", "owner_id": "custom", "kind": "app"}
            assert authors[2] == {"login": "another-string", "owner_id": "default", "kind": None}

    def test_downgrade_converts_objects_to_strings(self, setup_and_teardown):
        """Downgrade should convert object-format comment_authors back to strings."""
        engine = setup_and_teardown
        config = get_alembic_config()

        repo_id = uuid.uuid4()

        with engine.connect() as conn:
            conn.execute(
                text("""
                INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors)
                VALUES (:id, :name, true, 'default', CAST(:authors AS jsonb))
            """),
                {"id": repo_id, "name": "org/repo-downgrade", "authors": json.dumps(["user1", "user2"])},
            )
            conn.commit()

        command.upgrade(config, "005_owner_id")

        with engine.connect() as conn:
            result = conn.execute(text("SELECT comment_authors FROM repositories WHERE id = :id"), {"id": repo_id})
            row = result.fetchone()
            authors_after_upgrade = row[0]
            assert all(isinstance(a, dict) for a in authors_after_upgrade)

        command.downgrade(config, "004_finding_details_visibility")

        with engine.connect() as conn:
            result = conn.execute(text("SELECT comment_authors FROM repositories WHERE id = :id"), {"id": repo_id})
            row = result.fetchone()
            assert row is not None
            authors = row[0]

            assert authors == ["user1", "user2"]

            result = conn.execute(
                text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'repositories' AND column_name = 'owner_id'
            """)
            )
            assert result.fetchone() is None

    def test_downgrade_handles_mixed_list(self, setup_and_teardown):
        """Downgrade should normalize mixed lists (objects + strings) to plain strings."""
        engine = setup_and_teardown
        config = get_alembic_config()

        command.upgrade(config, "005_owner_id")

        repo_id = uuid.uuid4()
        mixed_authors = [
            {"login": "object-user", "owner_id": "custom", "kind": "app"},
            "string-user",
            {"login": "another-object", "owner_id": "default", "kind": None},
        ]

        with engine.connect() as conn:
            conn.execute(
                text("""
                INSERT INTO repositories (id, full_name, enabled, policy_profile, comment_authors, owner_id)
                VALUES (:id, :name, true, 'default', CAST(:authors AS jsonb), 'default')
            """),
                {"id": repo_id, "name": "org/repo-mixed-down", "authors": json.dumps(mixed_authors)},
            )
            conn.commit()

        command.downgrade(config, "004_finding_details_visibility")

        with engine.connect() as conn:
            result = conn.execute(text("SELECT comment_authors FROM repositories WHERE id = :id"), {"id": repo_id})
            row = result.fetchone()
            assert row is not None
            authors = row[0]

            assert authors == ["object-user", "string-user", "another-object"]
