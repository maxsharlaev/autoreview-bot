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

import logging
import os

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

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


DOWNGRADE_OPT_IN_ENV = "MIGRATION_007_DOWNGRADE_DROP_DUPLICATES"


def downgrade() -> None:
    """Remove owner_id from findings and restore the (pull_request_id, stable_id) constraint.

    After this migration several owners may hold a finding with the same stable_id on
    one pull request. The old constraint cannot hold such rows, so the downgrade refuses
    by default and reports how many (pull_request_id, stable_id) groups conflict.

    Set MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1 to delete the extra rows instead. For
    each conflicting group the row owned by the repository's current owner is kept
    (falling back to the oldest row); deleted finding ids are logged. Deleting a finding
    also deletes its finding_transitions (ON DELETE CASCADE).
    """
    conn = op.get_bind()

    conflict_groups = conn.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT 1
                FROM findings
                GROUP BY pull_request_id, stable_id
                HAVING COUNT(*) > 1
            ) AS conflicts
            """
        )
    ).scalar_one()

    if conflict_groups:
        opted_in = os.environ.get(DOWNGRADE_OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes"}
        if not opted_in:
            raise RuntimeError(
                f"Cannot downgrade 007_findings_owner_id: {conflict_groups} (pull_request_id, stable_id) "
                "group(s) have findings from more than one owner. Set "
                f"{DOWNGRADE_OPT_IN_ENV}=1 to keep only the repository owner's row (or the oldest) "
                "and delete the rest together with their transitions."
            )
        deleted = conn.execute(
            sa.text(
                """
                WITH ranked AS (
                    SELECT f.id,
                           ROW_NUMBER() OVER (
                               PARTITION BY f.pull_request_id, f.stable_id
                               ORDER BY (f.owner_id = r.owner_id) DESC, f.created_at ASC, f.id ASC
                           ) AS rn
                    FROM findings f
                    JOIN pull_requests p ON p.id = f.pull_request_id
                    JOIN repositories r ON r.id = p.repository_id
                )
                DELETE FROM findings
                WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
                RETURNING id, pull_request_id, owner_id, stable_id
                """
            )
        ).all()
        for row in deleted:
            logger.warning(
                "007 downgrade deleted finding id=%s pull_request_id=%s owner_id=%s stable_id=%s",
                row.id,
                row.pull_request_id,
                row.owner_id,
                row.stable_id,
            )
        logger.warning("007 downgrade deleted %d duplicate finding(s) in %d group(s)", len(deleted), conflict_groups)

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
