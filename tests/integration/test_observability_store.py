"""Durable traces, retention and audit integrity against the real store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.audit.protocols import AuditRecord
from domain.observability.models import SpanKind, SpanStatus, TraceEvent
from domain.policies.models import ActorKind
from domain.tasks.task import Task
from infrastructure.persistence.audit_repository import SqlAuditLog
from infrastructure.persistence.models import (
    ApprovalRow,
    AuditRow,
    LLMCallRow,
    ObjectiveRow,
    PlanRow,
    ScheduleRow,
    TaskRow,
    TraceEventRow,
)
from infrastructure.persistence.session import session_scope
from infrastructure.persistence.task_repository import SqlTaskRepository
from infrastructure.persistence.trace_repository import SqlTraceRepository


async def test_trace_round_trip_redacts_every_text_field_and_keeps_causality(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    traces = SqlTraceRepository(session_factory)
    trace_id, root, child = uuid4(), uuid4(), uuid4()
    await traces.emit(
        TraceEvent(
            trace_id=trace_id,
            span_id=root,
            kind=SpanKind.OBJECTIVE,
            status=SpanStatus.RUNNING,
            name="api_key=trace-canary-value",
            entity_type="objective",
            entity_id=str(trace_id),
            attributes={"authorization": "Bearer trace-canary-value"},
        )
    )
    await traces.emit(
        TraceEvent(
            trace_id=trace_id,
            span_id=child,
            parent_id=root,
            causation_id=root,
            kind=SpanKind.APPROVAL,
            status=SpanStatus.DENIED,
            name="Approval decision",
            entity_type="approval",
            entity_id=str(uuid4()),
        )
    )

    stored = await traces.get(trace_id)
    assert stored is not None
    assert [event.parent_id for event in stored.events] == [None, root]
    assert stored.first_failure is not None
    assert "canary" not in str(stored)


async def test_unknown_trace_schema_is_visible_but_never_interpreted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    trace_id = uuid4()
    async with session_scope(session_factory) as session:
        session.add(
            TraceEventRow(
                event_id=str(uuid4()),
                schema_version=99,
                trace_id=str(trace_id),
                span_id=str(uuid4()),
                kind="FUTURE_KIND",
                status="OK",
                name="future payload",
                attributes={"meaning": "unknown"},
            )
        )

    trace = await SqlTraceRepository(session_factory).get(trace_id)
    assert trace is not None
    [event] = trace.events
    assert event.status is SpanStatus.DEGRADED
    assert event.reason_code == "UNSUPPORTED_SCHEMA_VERSION"
    assert "future payload" not in str(event)


async def test_trace_retention_never_deletes_execution_or_audit_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    traces = SqlTraceRepository(session_factory)
    tasks = SqlTaskRepository(session_factory)
    audit = SqlAuditLog(session_factory)
    task = Task.create("Keep this authoritative row")
    await tasks.save(task)
    await audit.record(
        AuditRecord(
            action="read metadata",
            actor_kind=ActorKind.USER,
            result="SUCCESS",
            task_id=task.id,
        )
    )
    await traces.emit(
        TraceEvent(
            trace_id=task.id,
            span_id=uuid4(),
            kind=SpanKind.TASK,
            status=SpanStatus.RUNNING,
            name="Task execution",
            entity_type="task",
            entity_id=str(task.id),
            started_at=datetime.now(UTC) - timedelta(days=40),
        )
    )

    # The write path already enforces the same default age bound; explicit
    # maintenance is idempotent and still leaves authoritative evidence alone.
    assert await traces.prune(task.workspace_id, datetime.now(UTC) - timedelta(days=30)) == 0
    assert await tasks.get(task.id) is not None
    assert len(await audit.recent(task_id=task.id)) == 1


async def test_sql_trace_store_enforces_age_and_count_bounds_on_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    traces = SqlTraceRepository(session_factory, maximum_events_per_workspace=2)
    old = datetime.now(UTC) - timedelta(days=40)
    for index in range(4):
        await traces.emit(
            TraceEvent(
                trace_id=uuid4(),
                span_id=uuid4(),
                kind=SpanKind.TASK,
                status=SpanStatus.OK,
                name=f"Event {index}",
                started_at=old if index == 0 else datetime.now(UTC),
            )
        )

    async with session_scope(session_factory) as session:
        rows = list(await session.scalars(select(TraceEventRow).order_by(TraceEventRow.sequence)))
    assert [row.name for row in rows] == ["Event 2", "Event 3"]


async def test_projection_preserves_parallel_parents_schedule_escalation_approval_and_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    objective_id, plan_id = str(uuid4()), str(uuid4())
    first_id, second_id = str(uuid4()), str(uuid4())
    now = datetime.now(UTC)
    async with session_scope(session_factory) as session:
        session.add(
            ObjectiveRow(
                id=objective_id,
                text="not exposed",
                status="DONE",
                result={},
                finished_at=now,
            )
        )
        session.add(
            PlanRow(
                id=plan_id,
                objective_id=objective_id,
                status="DONE",
                definition={},
            )
        )
        for task_id in (first_id, second_id):
            session.add(
                TaskRow(
                    id=task_id,
                    plan_id=plan_id,
                    goal="not exposed",
                    status="COMPLETED",
                    result={
                        "summary": "not exposed",
                        "output": {},
                        "artifacts": [f"private/{task_id}.txt"],
                    },
                    updated_at=now,
                )
            )
        await session.flush()
        session.add(
            ScheduleRow(
                id=str(uuid4()),
                name="Daily report",
                request="not exposed",
                last_objective_id=objective_id,
                last_run_at=now,
            )
        )
        session.add(
            LLMCallRow(
                task_id=first_id,
                provider="local",
                model="strong",
                entry="reasoning",
                reason="retry needed a stronger model",
                escalation_level=1,
            )
        )
        session.add(
            ApprovalRow(
                id=str(uuid4()),
                task_id=first_id,
                action="write report",
                tool="fs.write",
                risk_level="MEDIUM",
                state="APPROVED",
                resolved_at=now,
                resolved_by="user",
            )
        )
        session.add(
            AuditRow(
                actor_kind="EMPLOYEE",
                task_id=second_id,
                action="send without a decision",
                tool="email.send",
                result="SUCCESS",
                details={"effect": "SEND"},
            )
        )

    trace = await SqlTraceRepository(session_factory).get(UUID(objective_id))
    assert trace is not None
    assert trace.run_kind.value == "SCHEDULE"
    by_kind: dict[SpanKind, list[TraceEvent]] = {}
    for event in trace.events:
        by_kind.setdefault(event.kind, []).append(event)
    [schedule] = by_kind[SpanKind.SCHEDULE]
    [objective] = by_kind[SpanKind.OBJECTIVE]
    [plan] = by_kind[SpanKind.PLAN]
    assert objective.parent_id == schedule.span_id
    assert {event.parent_id for event in by_kind[SpanKind.TASK]} == {plan.span_id}
    [escalation] = by_kind[SpanKind.ESCALATION]
    [model] = by_kind[SpanKind.MODEL]
    assert model.parent_id == escalation.span_id
    assert by_kind[SpanKind.APPROVAL][0].parent_id in {
        event.span_id for event in by_kind[SpanKind.TASK]
    }
    assert len(by_kind[SpanKind.ARTIFACT]) == 2
    assert any(event.attributes.get("unsafe_without_approval") for event in trace.events)
    assert "private/" not in str(trace)


async def test_audit_verifier_detects_change_and_deletion_including_the_head(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    audit = SqlAuditLog(session_factory)
    for month in (1, 2):
        await audit.record(
            AuditRecord(
                action=f"action {month}",
                actor_kind=ActorKind.USER,
                result="SUCCESS",
                timestamp=datetime(2026, month, 1, tzinfo=UTC),
            )
        )
    assert (await audit.verify()).valid

    async with session_scope(session_factory) as session:
        first = await session.scalar(select(AuditRow).order_by(AuditRow.id))
        assert first is not None
        await session.execute(
            update(AuditRow).where(AuditRow.id == first.id).values(action="changed")
        )
    changed = await audit.verify()
    assert not changed.valid
    assert changed.reason == "record hash mismatch"

    # Restore the row, then remove the current head. The checkpoint is outside
    # the chain precisely so deleting the last row cannot look like clean rotation.
    async with session_scope(session_factory) as session:
        await session.execute(
            update(AuditRow).where(AuditRow.id == first.id).values(action="action 1")
        )
    assert (await audit.verify()).valid
    async with session_scope(session_factory) as session:
        last = await session.scalar(select(AuditRow).order_by(AuditRow.id.desc()))
        assert last is not None
        await session.execute(delete(AuditRow).where(AuditRow.id == last.id))
    deleted = await audit.verify()
    assert not deleted.valid
    assert deleted.reason == "checkpoint mismatch"


async def test_trace_storage_failure_is_degradation_not_an_exception(
    tmp_path,
) -> None:
    from infrastructure.persistence.session import create_engine, create_session_factory

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'missing-schema.db'}")
    traces = SqlTraceRepository(create_session_factory(engine))
    await traces.emit(
        TraceEvent(
            trace_id=uuid4(),
            span_id=uuid4(),
            kind=SpanKind.TASK,
            status=SpanStatus.RUNNING,
            name="Still runs",
        )
    )
    assert not traces.health().available
    assert traces.health().dropped == 1
    await engine.dispose()
