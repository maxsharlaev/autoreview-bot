"""Add owner_id to findings table for cross-owner isolation.

Multi-owner M2: add owner_id to findings to prevent IntegrityError when a
repository changes ownership. The stable_id is deterministic (hash of
category+path+title), so owner A and owner B can have the same stable_id
for different contexts. The unique constraint changes from (pull_request_id,
stable_id) to (pull_request_id, owner_id, stable_id).

Backfills owner_id from the first_seen_run's owner_id.

Revision ID: 007_findings_owner_id
Revises: 006_comment_authors_objects
"""

import sqlalchemy as sa
from alembic import op

revision = "007_findings_owner_id"
down_revision = "006_comment_authors_objects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add owner_id to findings and change unique constraint.

    1. Add owner_id column with default 'default'
    2. Backfill owner_id from first_seen_run's owner_id
    3. Drop old unique constraint (pull_request_id, stable_id)
    4. Add new unique constraint (pull_request_id, owner_id, stable_id)
    """
    # Add owner_id column with server default
    op.add_column(
        "findings",
        sa.Column("owner_id", sa.String(64), nullable=False, server_default="default"),
    )

    # Backfill owner_id from the first_seen_run's owner_id
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE findings f
            SET owner_id = r.owner_id
            FROM review_runs r
            WHERE f.first_seen_run_id = r.id
              AND r.owner_id IS NOT NULL
              AND r.owner_id != 'default'
            """
        )
    )

    # Add index on owner_id for efficient filtering
    op.create_index("ix_findings_owner_id", "findings", ["owner_id"])

    # Drop old unique constraint
    op.drop_constraint("uq_findings_pr_stable", "findings", type_="unique")

    # Add new unique constraint including owner_id
    op.create_unique_constraint(
        "uq_findings_pr_owner_stable",
        "findings",
        ["pull_request_id", "owner_id", "stable_id"],
    )

    # Remove server default after backfill (column is now NOT NULL)
    op.alter_column("findings", "owner_id", server_default=None)


def downgrade() -> None:
    """Remove owner_id from findings and restore old unique constraint.

    Note: This downgrade may fail if there are findings with the same
    (pull_request_id, stable_id) but different owner_ids. In that case,
    the newer findings (by created_at) should be deleted first.
    """
    conn = op.get_bind()

    # Check for conflicts before downgrade
    conflict_check = conn.execute(
        sa.text(
            """
            SELECT pull_request_id, stable_id, COUNT(*) as cnt
            FROM findings
            GROUP BY pull_request_id, stable_id
            HAVING COUNT(*) > 1
            """
        )
    )
    conflicts = list(conflict_check)

    if conflicts:
        # Delete duplicate findings, keeping the oldest one (first_seen)
        for row in conflicts:
            pr_id, stable_id, _ = row
            conn.execute(
                sa.text(
                    """
                    DELETE FROM findings
                    WHERE id IN (
                        SELECT id FROM findings
                        WHERE pull_request_id = :pr_id AND stable_id = :stable_id
                        ORDER BY created_at DESC
                        OFFSET 1
                    )
                    """
                ),
                {"pr_id": pr_id, "stable_id": stable_id},
            )

    # Drop new unique constraint
    op.drop_constraint("uq_findings_pr_owner_stable", "findings", type_="unique")

    # Drop index
    op.drop_index("ix_findings_owner_id", "findings")

    # Drop owner_id column
    op.drop_column("findings", "owner_id")

    # Restore old unique constraint
    op.create_unique_constraint(
        "uq_findings_pr_stable",
        "findings",
        ["pull_request_id", "stable_id"],
    )
