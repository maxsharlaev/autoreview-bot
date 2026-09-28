"""Track bot-owned PR titles and their source state.

Revision ID: 002_bot_title
Revises: 001_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "002_bot_title"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pull_requests", sa.Column("bot_title", sa.String(length=512), nullable=True))
    op.add_column("pull_requests", sa.Column("bot_title_source_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "bot_title_source_hash")
    op.drop_column("pull_requests", "bot_title")
