"""What Phase 10 writes down, against a real SQLite file.

Two tables and one column, and each is here because losing it loses something
that has to survive the process: the record of what was done, the record of what
a workflow ran, and the deadline that closes a question nobody answered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.approvals.models import Approval, ApprovalRequest, ApprovalState
from domain.audit.protocols import AuditRecord
from domain.policies.models import ActorKind
from domain.workflows.definition import WorkflowTrigger
from domain.workflows.run import RunStatus, StepOutcome, WorkflowRun
from infrastructure.persistence.approval_repository import SqlApprovalRepository
from infrastructure.persistence.audit_repository import SqlAuditLog
from infrastructure.persistence.workflow_repository import SqlWorkflowRunRepository


def record(**extra) -> AuditRecord:
    return AuditRecord(
        action=extra.pop("action", "fs.write(path='notes.md')"),
        actor_kind=extra.pop("actor_kind", ActorKind.EMPLOYEE),
        result=extra.pop("result", "SUCCESS"),
        **extra,
    )


# --- The audit log ------------------------------------------------------------


async def test_an_action_survives_the_process_that_took_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    audit = SqlAuditLog(session_factory)
    task_id = uuid4()

    await audit.record(record(actor_id="researcher", task_id=task_id, tool="fs.write"))

    stored = await audit.recent()
    assert len(stored) == 1
    assert stored[0].actor_kind is ActorKind.EMPLOYEE
    assert stored[0].task_id == task_id


async def test_the_refusals_are_in_the_same_place_as_the_actions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The point of the table: a denied action has no tool call anywhere, so if
    it is not here it is nowhere."""
    audit = SqlAuditLog(session_factory)

    await audit.record(record(result="SUCCESS", action="fs.read(path='a')"))
    await audit.record(
        record(result="DENIED", action="mail.send(to='x')", details={"reason": "no_sending"})
    )

    results = {r.result for r in await audit.recent()}
    assert results == {"SUCCESS", "DENIED"}
    denied = next(r for r in await audit.recent() if r.result == "DENIED")
    assert denied.details["reason"] == "no_sending"


async def test_one_task_can_be_read_out_of_a_busy_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    audit = SqlAuditLog(session_factory)
    wanted, other = uuid4(), uuid4()

    await audit.record(record(task_id=wanted))
    await audit.record(record(task_id=other))

    assert [r.task_id for r in await audit.recent(task_id=wanted)] == [wanted]


async def test_a_broken_store_does_not_stop_the_work_it_was_recording(
    tmp_path,
) -> None:
    """A machine whose accountant has gone away still does the work. The
    alternative is a platform that stops when its logging does."""
    from infrastructure.persistence.session import create_engine, create_session_factory

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'no-schema.db'}")
    audit = SqlAuditLog(create_session_factory(engine))

    await audit.record(record())  # the table does not exist; this must not raise

    await engine.dispose()


# --- Workflow runs ------------------------------------------------------------


async def test_a_workflow_run_round_trips_with_what_each_step_produced(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runs = SqlWorkflowRunRepository(session_factory)
    task_id = uuid4()
    run = WorkflowRun.create(
        "weekly-report",
        workflow_version=3,
        trigger=WorkflowTrigger.MANUAL,
        inputs={"folder": "sales"},
    ).with_step(StepOutcome("survey", "organizer", task_id=task_id, succeeded=True, attempts=1))

    await runs.save(run)
    await runs.save(run.finished(RunStatus.COMPLETED, "1 step(s) completed."))

    stored = await runs.get(run.id)
    assert stored is not None
    assert stored.status is RunStatus.COMPLETED
    assert stored.inputs == {"folder": "sales"}
    assert stored.workflow_version == 3
    assert stored.steps[0].task_id == task_id
    assert stored.summary == "1 step(s) completed."


async def test_recent_runs_come_back_newest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runs = SqlWorkflowRunRepository(session_factory)
    older = WorkflowRun.create("a", started_at=datetime.now(UTC) - timedelta(hours=1))
    newer = WorkflowRun.create("b")

    await runs.save(older)
    await runs.save(newer)

    assert [run.workflow for run in await runs.recent()] == ["b", "a"]


# --- The deadline on a question -----------------------------------------------


async def test_a_question_nobody_answered_is_closed_in_the_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The process that asked is gone. The row is the only thing left."""
    from domain.tasks.task import Task
    from infrastructure.persistence.task_repository import SqlTaskRepository

    task = Task.create("do something")
    await SqlTaskRepository(session_factory).save(task)

    approvals = SqlApprovalRepository(session_factory)
    asked = ApprovalRequest.create(task_id=task.id, action="code.run(...)").expiring_in(60)
    await approvals.save(Approval(request=asked))

    closed = await approvals.expire_overdue(datetime.now(UTC) + timedelta(minutes=5))

    assert closed == 1
    stored = await approvals.get(asked.id)
    assert stored is not None and stored.state is ApprovalState.EXPIRED
    assert await approvals.list_pending() == []
