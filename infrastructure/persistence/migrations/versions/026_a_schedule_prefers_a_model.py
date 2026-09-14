"""A schedule can say which model its runs prefer.

Revision ID: 026
Revises: 025
Create Date: 2026-09-14

A request typed into the window could already prefer a model; a schedule could
not, so every run went wherever the router sent it - and the first real one went
to a free model that was overloaded at that hour. `model` is a catalog entry by
name, with a default of "" so every existing schedule keeps letting the router
decide.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(
            sa.Column("model", sa.String(120), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("model")
