"""Durable causal traces and a checkpointed audit hash chain.

Old audit rows are chained in their existing per-workspace order. No trace is
invented for old execution rows: the trace adapter projects those authoritative
rows and labels missing observability events as legacy data.

Revision ID: 042
Revises: 041
Create Date: 2026-09-17
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None

GENESIS_HASH = "0" * 64


def _utc(value: object) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if not moment.tzinfo:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _details(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def _hash(row: sa.RowMapping, previous: str) -> str:
    value = {
        "version": 1,
        "sequence": row["id"],
        "previous_hash": previous,
        "timestamp": _utc(row["ts"]),
        "workspace_id": row["workspace_id"],
        "actor_kind": row["actor_kind"],
        "actor_id": row["actor_id"],
        "task_id": row["task_id"],
        "assignment_id": row["assignment_id"],
        "action": row["action"],
        "tool": row["tool"],
        "model": row["model"],
        "result": row["result"],
        "cost_usd": row["cost_usd"],
        "latency_ms": row["latency_ms"],
        "details": _details(row["details"]),
    }
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def upgrade() -> None:
    op.add_column(
        "audit_log", sa.Column("chain_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "audit_log", sa.Column("chain_id", sa.String(length=32), nullable=False, server_default="")
    )
    op.add_column(
        "audit_log",
        sa.Column("previous_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "audit_log",
        sa.Column("record_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_table(
        "audit_checkpoints",
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "trace_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("span_id", sa.String(length=36), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("causation_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("entity_type", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("entity_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("event_id", name="uq_trace_events_event_id"),
    )
    op.create_index("ix_trace_events_trace", "trace_events", ["trace_id", "sequence"])
    op.create_index(
        "ix_trace_events_workspace", "trace_events", ["workspace_id", "started_at"]
    )
    op.create_index("ix_trace_events_entity", "trace_events", ["entity_type", "entity_id"])

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT * FROM audit_log ORDER BY workspace_id, id")).mappings()
    heads: dict[str, tuple[int, str, int, datetime]] = {}
    for row in rows:
        workspace = str(row["workspace_id"])
        previous = heads.get(workspace, (0, GENESIS_HASH, 0, datetime.now(UTC)))[1]
        digest = _hash(row, previous)
        chain_id = _utc(row["ts"])[:7]
        bind.execute(
            sa.text(
                "UPDATE audit_log SET chain_id=:chain_id, previous_hash=:previous, "
                "record_hash=:digest WHERE id=:sequence"
            ),
            {
                "chain_id": chain_id,
                "previous": previous,
                "digest": digest,
                "sequence": row["id"],
            },
        )
        old_count = heads.get(workspace, (0, "", 0, datetime.now(UTC)))[2]
        heads[workspace] = (
            int(row["id"]),
            digest,
            old_count + 1,
            datetime.now(UTC),
        )
    for workspace, (sequence, digest, count, now) in heads.items():
        bind.execute(
            sa.text(
                "INSERT INTO audit_checkpoints "
                "(workspace_id, sequence, record_hash, record_count, updated_at) "
                "VALUES (:workspace, :sequence, :digest, :count, :updated)"
            ),
            {
                "workspace": workspace,
                "sequence": sequence,
                "digest": digest,
                "count": count,
                "updated": now.replace(tzinfo=None),
            },
        )


def downgrade() -> None:
    op.drop_index("ix_trace_events_entity", table_name="trace_events")
    op.drop_index("ix_trace_events_workspace", table_name="trace_events")
    op.drop_index("ix_trace_events_trace", table_name="trace_events")
    op.drop_table("trace_events")
    op.drop_table("audit_checkpoints")
    op.drop_column("audit_log", "record_hash")
    op.drop_column("audit_log", "previous_hash")
    op.drop_column("audit_log", "chain_id")
    op.drop_column("audit_log", "chain_version")
