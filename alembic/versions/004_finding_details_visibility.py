"""Add finding detail provenance tracking.

Revision ID: 004_finding_details_visibility
Revises: 003_comment_authors
"""

import sqlalchemy as sa
from alembic import op

revision = "004_finding_details_visibility"
down_revision = "003_comment_authors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("details_visibility", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("findings", "details_visibility")
