"""A thread works in a folder of its own, or in one a person chose.

Revision ID: 028
Revises: 027
Create Date: 2026-09-15

Every request in a workspace read and wrote one directory, so ten threads left
their files side by side and "the file from yesterday's news run" was a search.
`conversations.folder` is the directory a thread's file tools see; empty is what
every earlier thread has, and reads as the workspace's root, which is where
their files already are.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column("folder", sa.String(1024), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("folder")
