"""How a request was asked to be carried out is kept, on it and on a schedule.

Revision ID: 027
Revises: 026
Create Date: 2026-09-14

Approvals and a preferred model were chosen under the field and never written
down, so a thread reopened showed "Ask me" and "Auto" whatever its last request
had been set to - and a schedule's thread showed that too, under a schedule set
to a particular model. `objectives.directions` records them per request (empty
reads as the defaults, which is what every earlier request was), and
`schedules.approvals` gives a schedule the choice its model already had.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("objectives") as batch:
        batch.add_column(sa.Column("directions", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(
            sa.Column("approvals", sa.String(8), nullable=False, server_default="ASK")
        )


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("approvals")
    with op.batch_alter_table("objectives") as batch:
        batch.drop_column("directions")
