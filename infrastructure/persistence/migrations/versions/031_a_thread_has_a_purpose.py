"""Distinguish conversation-first threads from delegated work.

Revision ID: 031
Revises: 030
Create Date: 2026-09-17

Existing threads are tasks because that is the only entry point the product
offered before this column. ASK is chosen explicitly when a person opens the
new, conversation-first surface.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(8), nullable=False, server_default="TASK")
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("kind")
