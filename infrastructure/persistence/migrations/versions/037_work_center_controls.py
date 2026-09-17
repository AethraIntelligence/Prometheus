"""Work Center keeps pauses and assignment explanations.

Revision ID: 037
Revises: 036
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None

TASK_CONSTRAINT = "ck_tasks_status"
OBJECTIVE_CONSTRAINT = "ck_objectives_status"
TASK_BEFORE = (
    "CREATED",
    "PLANNING",
    "RUNNING",
    "WAITING_FOR_TOOL",
    "WAITING_FOR_APPROVAL",
    "VERIFYING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)
OBJECTIVE_BEFORE = (
    "RECEIVED",
    "PLANNING",
    "RUNNING",
    "DONE",
    "FAILED",
    "ESCALATED",
    "CANCELLED",
)


def _constraint(table: str, name: str, statuses: tuple[str, ...]) -> None:
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint(name, type_="check")
    with op.batch_alter_table(table) as batch:
        batch.create_check_constraint(name, "status IN ('" + "','".join(statuses) + "')")


def _suspend_sqlite_foreign_keys() -> bool:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return False
    enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
    if enabled:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    return enabled


def _restore_sqlite_foreign_keys(enabled: bool) -> None:
    if enabled:
        op.get_bind().exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    # SQLite implements a CHECK change by replacing the table. Foreign keys
    # must be suspended around that replacement or its ON DELETE rules erase
    # the task's assignments, events and approval history with the old table.
    foreign_keys = _suspend_sqlite_foreign_keys()
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(
            sa.Column("assignment_reason", sa.Text(), nullable=False, server_default="")
        )
    _constraint("tasks", TASK_CONSTRAINT, (*TASK_BEFORE, "PAUSED"))
    _constraint("objectives", OBJECTIVE_CONSTRAINT, (*OBJECTIVE_BEFORE, "PAUSED"))
    _restore_sqlite_foreign_keys(foreign_keys)


def downgrade() -> None:
    foreign_keys = _suspend_sqlite_foreign_keys()
    op.execute("UPDATE tasks SET status = 'RUNNING' WHERE status = 'PAUSED'")
    op.execute("UPDATE objectives SET status = 'RUNNING' WHERE status = 'PAUSED'")
    _constraint("tasks", TASK_CONSTRAINT, TASK_BEFORE)
    _constraint("objectives", OBJECTIVE_CONSTRAINT, OBJECTIVE_BEFORE)
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("assignment_reason")
    _restore_sqlite_foreign_keys(foreign_keys)
