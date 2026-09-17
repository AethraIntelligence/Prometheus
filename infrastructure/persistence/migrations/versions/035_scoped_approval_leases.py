"""Approvals can create exact, revocable capability leases.

Revision ID: 035
Revises: 034
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("subject", sa.String(128), nullable=False, server_default=""))
        batch.add_column(sa.Column("resource", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("limits", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("preview", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(
            sa.Column("policy_source", sa.String(128), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("grant_kind", sa.String(16), nullable=False, server_default="ONCE")
        )
        batch.add_column(sa.Column("lease_id", sa.String(36), nullable=True))

    op.create_table(
        "capability_leases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("grant_kind", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "approval_id",
            sa.String(36),
            sa.ForeignKey("approvals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "grant_kind IN ('TASK','PERSISTENT')",
            name="ck_capability_leases_grant_kind",
        ),
    )
    op.create_index(
        "ix_capability_leases_active",
        "capability_leases",
        ["workspace_id", "revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_capability_leases_match",
        "capability_leases",
        ["workspace_id", "subject", "action", "resource"],
    )


def downgrade() -> None:
    op.drop_table("capability_leases")
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("lease_id")
        batch.drop_column("grant_kind")
        batch.drop_column("policy_source")
        batch.drop_column("preview")
        batch.drop_column("limits")
        batch.drop_column("resource")
        batch.drop_column("subject")
