"""A schedule made in the window writes its runs into a thread.

Revision ID: 025
Revises: 024
Create Date: 2026-09-14

Until now a scheduled objective carried no conversation, which was right for the
terminal that created every schedule and wrong the moment the window could: the
work happened, and the only surface a window user reads had no place to show it.
`conversation_id` is a label, as it is on `objectives` (migration 012) - nullable,
no foreign key - so every existing schedule keeps meaning what it meant.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(sa.Column("conversation_id", sa.String(36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("conversation_id")
