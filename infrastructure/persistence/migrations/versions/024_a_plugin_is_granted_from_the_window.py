"""Who may use an integration can be said on the integration.

Revision ID: 024
Revises: 023
Create Date: 2026-09-14

Until now the only grant was `integrations:` in an employee's own file, which is
right for a declaration and out of reach for a person installing a plugin from
the window. `granted_to` is the second way, kept on this machine's record: the
names of employees who may use it. Like the file, it can only add - an employee
it names gains the integration's tools and nothing is taken from anybody.

A JSON list with a default of `[]`, so every existing row keeps meaning exactly
what it meant: granted to whoever's file says so, and to nobody else.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("integrations") as batch:
        batch.add_column(
            sa.Column("granted_to", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("integrations") as batch:
        batch.drop_column("granted_to")
