"""Assignments keep why they were made and what the manager made of the result;
recurring processes are kept as suggestions a person decides on.

Existing assignments get an empty decision and no verdict. Neither was recorded
when they were made, and writing one now would be a record of something that
did not happen: readers treat an empty decision as unrecorded and derive a
verdict from the task row, marked as derived.

Columns are added one at a time for the reason migration 039 gives.

Revision ID: 041
Revises: 040
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

_ASSIGNMENT_COLUMNS = (
    sa.Column("decision", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("acceptance", sa.JSON(), nullable=True),
)


def upgrade() -> None:
    for column in _ASSIGNMENT_COLUMNS:
        op.add_column("task_assignments", column)

    op.create_table(
        "workflow_suggestions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("pattern_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("dismissed_sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(), nullable=True),
        sa.Column("workflow_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "fingerprint", name="uq_workflow_suggestions_fingerprint"
        ),
    )


def downgrade() -> None:
    op.drop_table("workflow_suggestions")
    for column in reversed(_ASSIGNMENT_COLUMNS):
        op.execute(f"ALTER TABLE task_assignments DROP COLUMN {column.name}")
