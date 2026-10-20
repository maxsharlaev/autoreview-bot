"""Add progress status, comment URL and sensitive-content flag to review runs.

- progress_status (JSONB, nullable): check run / commit status bookkeeping for issue #7.
- comment_url (nullable): URL of the review comment the run published.
- has_sensitive_content (nullable boolean): whether the published comment, before the
  public repository disclosure policy, would contain security finding details or Jira
  text. NULL means unknown: existing rows are not backfilled.

All three columns are nullable without defaults, so the upgrade is a metadata-only change
and the downgrade simply drops them.

Revision ID: 008_review_run_progress
Revises: 007_findings_owner_id
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "008_review_run_progress"
down_revision = "007_findings_owner_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("progress_status", postgresql.JSONB(), nullable=True))
    op.add_column("review_runs", sa.Column("comment_url", sa.String(512), nullable=True))
    op.add_column("review_runs", sa.Column("has_sensitive_content", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "has_sensitive_content")
    op.drop_column("review_runs", "comment_url")
    op.drop_column("review_runs", "progress_status")
