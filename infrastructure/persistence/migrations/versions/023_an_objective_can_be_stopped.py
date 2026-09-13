"""An objective a person stopped says so.

Revision ID: 023
Revises: 022
Create Date: 2026-09-13

Until now stopping an objective cancelled the coroutine carrying it and wrote
nothing, so the row stayed at whatever stage it had reached - RECEIVED, usually.
The window reads an unanswered objective as work in progress, which is right for
work in progress and wrong for work somebody stopped: the thread went on saying
"Reading your request..." through every restart, because the process that could
have finished it was gone and nothing else was allowed to.

`CANCELLED` is a terminal status of its own rather than FAILED with a note,
for the reason `Task` has one: nothing went wrong. The CHECK constraint lists the
statuses by value, so it is dropped and put back with the new list, which on
SQLite rebuilds the table.
"""

from __future__ import annotations

from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_objectives_status"
BEFORE = ("RECEIVED", "PLANNING", "RUNNING", "DONE", "FAILED", "ESCALATED")
AFTER = (*BEFORE, "CANCELLED")


def _allow(statuses: tuple[str, ...]) -> None:
    with op.batch_alter_table("objectives") as batch:
        batch.drop_constraint(CONSTRAINT, type_="check")
    with op.batch_alter_table("objectives") as batch:
        batch.create_check_constraint(CONSTRAINT, "status IN ('" + "','".join(statuses) + "')")


def upgrade() -> None:
    _allow(AFTER)


def downgrade() -> None:
    # A stopped objective is, as far as the older schema can say, a failed one.
    op.execute("UPDATE objectives SET status = 'FAILED' WHERE status = 'CANCELLED'")
    _allow(BEFORE)
