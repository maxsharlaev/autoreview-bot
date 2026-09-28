"""Remember GitHub identities that published sticky comments.

Revision ID: 003_comment_authors
Revises: 002_bot_title
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003_comment_authors"
down_revision = "002_bot_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column(
            "comment_authors",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("repositories", "comment_authors")
