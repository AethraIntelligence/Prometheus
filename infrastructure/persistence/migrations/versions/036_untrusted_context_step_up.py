"""Approval requests retain untrusted context provenance.

Revision ID: 036
Revises: 035
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(
            sa.Column(
                "requires_explicit_confirmation",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("context_sources", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("context_sources")
        batch.drop_column("requires_explicit_confirmation")
