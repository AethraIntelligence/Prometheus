"""A thread keeps its approval mode when it is reopened.

Revision ID: 029
Revises: 028
Create Date: 2026-09-17

The composer allowed the approval mode to change inside an existing thread,
but only held that change in React state. A nullable value preserves the old
fallback to the latest objective for existing rows until they are next used.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("approvals", sa.String(8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("approvals")
