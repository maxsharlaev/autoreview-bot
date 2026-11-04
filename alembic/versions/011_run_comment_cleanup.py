"""Admin audit of manual review-comment cleanup on review_runs.

Revision ID: 011_run_comment_cleanup
Revises: 010_finding_admin_status
"""

import sqlalchemy as sa
from alembic import op

revision = "011_run_comment_cleanup"
down_revision = "010_finding_admin_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("comment_cleaned_by", sa.String(length=128), nullable=True))
    op.add_column("review_runs", sa.Column("comment_cleaned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("review_runs", sa.Column("comment_cleanup_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "comment_cleanup_reason")
    op.drop_column("review_runs", "comment_cleaned_at")
    op.drop_column("review_runs", "comment_cleaned_by")
