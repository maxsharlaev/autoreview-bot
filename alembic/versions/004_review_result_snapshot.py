"""Track finding detail provenance and preserve private review results.

Revision ID: 004_review_result_snapshot
Revises: 003_comment_authors
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "004_review_result_snapshot"
down_revision = "003_comment_authors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing details have unknown provenance and must be treated as private.
    op.add_column("repositories", sa.Column("last_visibility", sa.String(length=16), nullable=True))
    op.add_column("findings", sa.Column("details_visibility", sa.String(length=16), nullable=True))
    op.create_table(
        "review_result_snapshots",
        sa.Column("review_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("review_run_id"),
    )


def downgrade() -> None:
    op.drop_table("review_result_snapshots")
    op.drop_column("findings", "details_visibility")
    op.drop_column("repositories", "last_visibility")
