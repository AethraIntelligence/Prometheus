"""Pin schedules to immutable workflow versions and enrich run history.

Revision ID: 038
Revises: 037
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_runs") as batch:
        batch.add_column(
            sa.Column("workflow_version", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(
            sa.Column("workflow_name", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("workflow_version", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("workflow_inputs", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(
            sa.Column("workflow_snapshot", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(
            sa.Column(
                "retry_policy",
                sa.String(length=16),
                nullable=False,
                server_default="DECLARED",
            )
        )
        batch.add_column(
            sa.Column(
                "misfire_policy",
                sa.String(length=16),
                nullable=False,
                server_default="COALESCE",
            )
        )
        batch.add_column(
            sa.Column(
                "concurrency_policy",
                sa.String(length=16),
                nullable=False,
                server_default="SKIP",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("concurrency_policy")
        batch.drop_column("misfire_policy")
        batch.drop_column("retry_policy")
        batch.drop_column("workflow_snapshot")
        batch.drop_column("workflow_inputs")
        batch.drop_column("workflow_version")
        batch.drop_column("workflow_name")
    with op.batch_alter_table("workflow_runs") as batch:
        batch.drop_column("workflow_version")
