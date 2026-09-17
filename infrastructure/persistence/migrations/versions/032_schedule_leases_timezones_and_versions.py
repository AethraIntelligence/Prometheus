"""Make proactive runs safe across processes and daylight-saving changes.

Revision ID: 032
Revises: 031
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(sa.Column("timezone", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("lease_owner", sa.String(64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
        batch.drop_column("version")
        batch.drop_column("timezone")
