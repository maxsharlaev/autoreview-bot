"""Convert comment_authors to object format.

Multi-owner M2: convert comment_authors from list of strings to list of objects
with {login, owner_id, kind} structure. This enables identity tracking per owner.

Note: After this migration, an image older than this PR cannot read comment_authors
correctly without first running the downgrade.

Revision ID: 006_comment_authors_objects
Revises: 005_owner_id
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "006_comment_authors_objects"
down_revision = "005_owner_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Convert comment_authors from list of strings to list of objects.

    Each string login becomes {login: <login>, owner_id: 'default', kind: null}.
    Objects are passed through unchanged (tolerant upgrade).
    """
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT id, comment_authors, owner_id FROM repositories WHERE comment_authors IS NOT NULL")
    )
    rows = list(result)

    for row in rows:
        repo_id = row[0]
        authors = row[1]
        repo_owner_id = row[2] or "default"

        if not authors or not isinstance(authors, list):
            continue

        new_authors = []
        needs_update = False
        for author in authors:
            if isinstance(author, str):
                new_authors.append(
                    {
                        "login": author,
                        "owner_id": repo_owner_id,
                        "kind": None,
                    }
                )
                needs_update = True
            elif isinstance(author, dict) and "login" in author:
                new_authors.append(author)
            # Skip invalid entries

        if needs_update:
            conn.execute(
                sa.text("UPDATE repositories SET comment_authors = CAST(:authors AS jsonb) WHERE id = :id"),
                {"authors": json.dumps(new_authors), "id": repo_id},
            )


def downgrade() -> None:
    """Convert comment_authors from list of objects back to list of strings.

    Objects {login: <login>, ...} become just the login string.
    Strings are passed through unchanged (tolerant downgrade).
    """
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT id, comment_authors FROM repositories WHERE comment_authors IS NOT NULL"))
    rows = list(result)

    for row in rows:
        repo_id = row[0]
        authors = row[1]

        if not authors or not isinstance(authors, list):
            continue

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
