"""Durable tool-call intents for at-most-once external actions.

Revision ID: 033
Revises: 032
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tool_calls") as batch:
        batch.add_column(sa.Column("call_id", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.create_unique_constraint("uq_tool_calls_task_call", ["task_id", "call_id"])


def downgrade() -> None:
    with op.batch_alter_table("tool_calls") as batch:
        batch.drop_constraint("uq_tool_calls_task_call", type_="unique")
        batch.drop_column("completed")
        batch.drop_column("call_id")
