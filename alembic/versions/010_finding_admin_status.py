"""Admin audit columns on findings (who/when/why the status was last changed by an admin).

Revision ID: 010_finding_admin_status
Revises: 009_admin_users
"""

import sqlalchemy as sa
from alembic import op

revision = "010_finding_admin_status"
down_revision = "009_admin_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("status_changed_by", sa.String(length=128), nullable=True))
    op.add_column("findings", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("findings", sa.Column("status_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("findings", "status_reason")
    op.drop_column("findings", "status_changed_at")
    op.drop_column("findings", "status_changed_by")
