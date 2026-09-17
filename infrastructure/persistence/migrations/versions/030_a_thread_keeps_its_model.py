"""A thread keeps its preferred model when it is reopened.

Revision ID: 030
Revises: 029
Create Date: 2026-09-17

Like approval mode, the model selector can change between requests. Keeping it
on the conversation makes that change durable without rewriting the directions
recorded on an earlier objective.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("model", sa.String(120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("model")
