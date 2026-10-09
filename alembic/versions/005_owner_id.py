"""Add owner_id columns and migrate comment_authors format.

Multi-owner M1: owner_id columns with server_default='default' on repositories,
review_runs, and digest_runs. Data migration for comment_authors from list of
strings to list of objects with tolerant reader support.

Revision ID: 005_owner_id
Revises: 004_finding_details_visibility
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "005_owner_id"
down_revision = "004_finding_details_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add owner_id column to repositories with server_default for backward compatibility
    op.add_column(
        "repositories",
        sa.Column(
            "owner_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
    )
    op.create_index("ix_repositories_owner_id", "repositories", ["owner_id"], unique=False)

    # Add owner_id column to review_runs
    op.add_column(
        "review_runs",
        sa.Column(
            "owner_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
    )
    op.create_index("ix_review_runs_owner_id", "review_runs", ["owner_id"], unique=False)

    # Add owner_id column to digest_runs
    op.add_column(
        "digest_runs",
        sa.Column(
            "owner_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
    )

    # Data migration: convert comment_authors from list of strings to list of objects
    # Old format: ["login1", "login2"]
    # New format: [{"login": "login1", "owner_id": "default", "kind": null}, ...]
    #
    # This migration is tolerant: if comment_authors already contains objects, it skips them.
    # The reader code must also be tolerant of both formats during the rollout period.
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT id, comment_authors FROM repositories WHERE comment_authors IS NOT NULL"))
    rows = list(result)

    for row in rows:
        repo_id = row[0]
        authors = row[1]

        if not authors or not isinstance(authors, list):
            continue

        # Check if already migrated (first element is an object)
        if authors and isinstance(authors[0], dict):
            continue

        # Convert strings to objects
        new_authors = []
        for author in authors:
            if isinstance(author, str):
                new_authors.append(
                    {
                        "login": author,
                        "owner_id": "default",
                        "kind": None,
                    }
                )
            elif isinstance(author, dict):
                new_authors.append(author)

        conn.execute(
            sa.text("UPDATE repositories SET comment_authors = :authors::jsonb WHERE id = :id"),
            {"authors": json.dumps(new_authors), "id": repo_id},
        )


def downgrade() -> None:
    # Revert comment_authors to list of strings first (before dropping columns)
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT id, comment_authors FROM repositories WHERE comment_authors IS NOT NULL"))
    rows = list(result)

    for row in rows:
        repo_id = row[0]
        authors = row[1]

        if not authors or not isinstance(authors, list):
            continue

        # Check if it's in the old format already (first element is a string)
        if authors and isinstance(authors[0], str):
            continue

        # Convert objects back to strings
        new_authors = []
        for author in authors:
            if isinstance(author, dict) and "login" in author:
                new_authors.append(author["login"])
            elif isinstance(author, str):
                new_authors.append(author)

        conn.execute(
            sa.text("UPDATE repositories SET comment_authors = :authors::jsonb WHERE id = :id"),
            {"authors": json.dumps(new_authors), "id": repo_id},
        )

    # Remove owner_id index and column from repositories
    op.drop_index("ix_repositories_owner_id", table_name="repositories")
    op.drop_column("repositories", "owner_id")

    # Remove owner_id index and column from review_runs
    op.drop_index("ix_review_runs_owner_id", table_name="review_runs")
    op.drop_column("review_runs", "owner_id")

    # Remove owner_id column from digest_runs
    op.drop_column("digest_runs", "owner_id")
