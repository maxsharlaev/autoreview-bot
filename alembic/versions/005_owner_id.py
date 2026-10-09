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
    # This migration normalizes every element (no first-element heuristic).
    # Mixed lists (strings + objects) are fully converted.
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT id, comment_authors FROM repositories WHERE comment_authors IS NOT NULL"))
    rows = list(result)

    for row in rows:
        repo_id = row[0]
        authors = row[1]

        if not authors or not isinstance(authors, list):
            continue

        # Normalize every element: strings -> objects, objects pass through
        new_authors = []
        needs_update = False
        for author in authors:
            if isinstance(author, str):
                new_authors.append({"login": author, "owner_id": "default", "kind": None})
                needs_update = True
            elif isinstance(author, dict):
                new_authors.append(author)
            # Skip invalid entries

        if needs_update:
            conn.execute(
                sa.text("UPDATE repositories SET comment_authors = CAST(:authors AS jsonb) WHERE id = :id"),
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

        # Normalize every element: objects -> strings, strings pass through
        new_authors = []
        needs_update = False
        for author in authors:
            if isinstance(author, dict) and "login" in author:
                new_authors.append(author["login"])
                needs_update = True
            elif isinstance(author, str):
                new_authors.append(author)
            # Skip invalid entries

        if needs_update:
            conn.execute(
                sa.text("UPDATE repositories SET comment_authors = CAST(:authors AS jsonb) WHERE id = :id"),
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
