"""Plans keep the tasks they have not started yet.

Revision ID: 034
Revises: 033
Create Date: 2026-09-17

A plan used to keep only its row and dependency edges. Task rows appeared when
delegation began, so a crash after planning but before a task started erased
the only copy of that task. The definition is immutable recovery state; live
task rows remain the source of truth once execution starts.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.add_column(
            sa.Column("definition", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.drop_column("definition")
