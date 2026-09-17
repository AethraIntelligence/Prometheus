"""Model contracts carry privacy and latency; model calls carry why they were routed.

Existing catalog entries served by this machine are LOCAL and every other entry
is REMOTE - the assumption the router makes for an entry that does not say.
Existing call records get empty reasons: what was decided then was never kept,
and inventing it now would be a record of something that did not happen.

Revision ID: 040
Revises: 039
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None

_ENTRY_COLUMNS = (
    sa.Column("privacy", sa.String(length=8), nullable=False, server_default="REMOTE"),
    sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
)
_CALL_COLUMNS = (
    sa.Column("task_kind", sa.String(length=16), nullable=False, server_default=""),
    sa.Column("entry", sa.String(length=120), nullable=False, server_default=""),
    sa.Column("reason", sa.Text(), nullable=False, server_default=""),
    sa.Column("escalation_level", sa.Integer(), nullable=False, server_default="0"),
)


def upgrade() -> None:
    for column in _ENTRY_COLUMNS:
        op.add_column("model_entries", column)
    op.execute("UPDATE model_entries SET privacy = 'LOCAL' WHERE provider = 'local'")
    for column in _CALL_COLUMNS:
        op.add_column("llm_calls", column)


def downgrade() -> None:
    for column in reversed(_CALL_COLUMNS):
        op.execute(f"ALTER TABLE llm_calls DROP COLUMN {column.name}")
    for column in reversed(_ENTRY_COLUMNS):
        op.execute(f"ALTER TABLE model_entries DROP COLUMN {column.name}")
