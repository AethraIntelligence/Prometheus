"""Memory provenance and revision, memory uses, and thread session briefs.

Existing memories are given the provenance their rows already imply rather than
a blanket "unknown": a note a person added is STATED, a preference read out of a
request is STATED at a lower confidence, a task's outcome is REPORTED and names
its task, a consolidated summary is INFERRED. What cannot be told stays INFERRED
at 0.5, which ranks it below a fact without hiding it.

Columns are added one at a time rather than through a batch rebuild: SQLite
rebuilds a table by copying it, and the triggers that keep the text index in
step with memory_items would not survive the copy.

Revision ID: 039
Revises: 038
Create Date: 2026-09-17
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None

_NEW_COLUMNS = (
    sa.Column("basis", sa.String(length=16), nullable=False, server_default="INFERRED"),
    sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
    sa.Column("status", sa.String(length=16), nullable=False, server_default="ACTIVE"),
    sa.Column("superseded_by", sa.String(length=36), nullable=True),
    sa.Column("revised_at", sa.DateTime(), nullable=True),
    sa.Column("contradicts", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("source_kind", sa.String(length=16), nullable=False, server_default="UNKNOWN"),
    sa.Column("source_ref", sa.String(length=64), nullable=False, server_default=""),
    sa.Column("source_label", sa.Text(), nullable=False, server_default=""),
    sa.Column("derived_from", sa.JSON(), nullable=False, server_default="[]"),
)


def upgrade() -> None:
    for column in _NEW_COLUMNS:
        op.add_column("memory_items", column)
    op.create_index("ix_memory_items_status", "memory_items", ["workspace_id", "status"])

    op.create_table(
        "memory_uses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("objective_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("reader", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_memory_uses_objective", "memory_uses", ["objective_id", "used_at"])
    op.create_index("ix_memory_uses_task", "memory_uses", ["task_id", "used_at"])
    op.create_index("ix_memory_uses_memory", "memory_uses", ["memory_id", "used_at"])

    op.create_table(
        "conversation_sessions",
        sa.Column("conversation_id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("goal_brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("stages", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("resolved_questions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    _backfill()


def _backfill() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, kind, content, task_id, metadata FROM memory_items")
    ).all()
    for item_id, kind, content, task_id, raw in rows:
        meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        basis, confidence, source_kind, source_ref, label = "INFERRED", 0.5, "UNKNOWN", "", ""
        if meta.get("source") == "person":
            basis, confidence, source_kind = "STATED", 1.0, "PERSON"
        elif str(content).startswith("The user prefers:"):
            basis, confidence, source_kind = "STATED", 0.8, "OBJECTIVE"
            label = str(meta.get("source", ""))[:200]
        elif meta.get("consolidated"):
            basis, confidence, source_kind = "INFERRED", 0.5, "CONSOLIDATION"
        elif task_id and kind == "SEMANTIC":
            basis, confidence, source_kind, source_ref = "OBSERVED", 0.9, "TASK", task_id
        elif task_id:
            status = meta.get("status")
            confidence = 0.75 if status == "COMPLETED" else 0.5
            basis, source_kind, source_ref = "REPORTED", "TASK", task_id
        bind.execute(
            sa.text(
                "UPDATE memory_items SET basis = :basis, confidence = :confidence, "
                "source_kind = :kind, source_ref = :ref, source_label = :label "
                "WHERE id = :id"
            ),
            {
                "basis": basis,
                "confidence": confidence,
                "kind": source_kind,
                "ref": source_ref or "",
                "label": label,
                "id": item_id,
            },
        )


def downgrade() -> None:
    op.drop_table("conversation_sessions")
    op.drop_index("ix_memory_uses_memory", table_name="memory_uses")
    op.drop_index("ix_memory_uses_task", table_name="memory_uses")
    op.drop_index("ix_memory_uses_objective", table_name="memory_uses")
    op.drop_table("memory_uses")
    op.drop_index("ix_memory_items_status", table_name="memory_items")
    # Plain DROP COLUMN on both dialects, for the reason the upgrade adds them
    # one by one: a batch rebuild would lose the text index's triggers.
    for column in reversed(_NEW_COLUMNS):
        op.execute(f"ALTER TABLE memory_items DROP COLUMN {column.name}")
