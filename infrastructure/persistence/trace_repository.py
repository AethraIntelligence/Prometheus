"""Durable trace events plus projections of authoritative execution rows."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.observability.models import (
    TRACE_SCHEMA_VERSION,
    RunKind,
    SpanKind,
    SpanStatus,
    TraceEvent,
    TraceView,
    stable_span_id,
)
from domain.observability.protocols import TraceHealth
from domain.secrets.models import redact
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.observability.logging import get_logger
from infrastructure.persistence.models import (
    ApprovalRow,
    AuditRow,
    LLMCallRow,
    ObjectiveRow,
    PlanRow,
    ScheduleRow,
    TaskAssignmentRow,
    TaskEventRow,
    TaskRow,
    ToolCallRow,
    TraceEventRow,
    WorkflowRunRow,
)
from infrastructure.persistence.session import session_scope

log = get_logger(__name__)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _status(value: str) -> SpanStatus:
    if value in {"DONE", "COMPLETED", "SUCCESS", "APPROVED"}:
        return SpanStatus.OK
    if value in {"FAILED", "FAILURE", "REJECTED", "EXPIRED"}:
        return SpanStatus.ERROR if value not in {"REJECTED", "EXPIRED"} else SpanStatus.DENIED
    if value == "CANCELLED":
        return SpanStatus.CANCELLED
    if value in {"RUNNING", "PLANNING", "VERIFYING", "WAITING_FOR_TOOL", "WAITING_FOR_APPROVAL"}:
        return SpanStatus.RUNNING
    return SpanStatus.UNSET


def _row_event(row: TraceEventRow) -> TraceEvent:
    if row.schema_version != TRACE_SCHEMA_VERSION:
        return TraceEvent(
            event_id=UUID(row.event_id),
            schema_version=row.schema_version,
            trace_id=UUID(row.trace_id),
            span_id=UUID(row.span_id),
            parent_id=UUID(row.parent_id) if row.parent_id else None,
            workspace_id=WorkspaceId(row.workspace_id),
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            kind=SpanKind.RECOVERY,
            status=SpanStatus.DEGRADED,
            name="Unknown trace schema",
            reason_code="UNSUPPORTED_SCHEMA_VERSION",
            started_at=_aware(row.started_at),
            attributes={"schema_version": row.schema_version, "content": "not interpreted"},
            sequence=row.sequence,
        )
    try:
        kind = SpanKind(row.kind)
        status = SpanStatus(row.status)
    except ValueError:
        kind = SpanKind.RECOVERY
        status = SpanStatus.DEGRADED
    return TraceEvent(
        event_id=UUID(row.event_id),
        schema_version=row.schema_version,
        trace_id=UUID(row.trace_id),
        span_id=UUID(row.span_id),
        parent_id=UUID(row.parent_id) if row.parent_id else None,
        correlation_id=row.correlation_id,
        causation_id=UUID(row.causation_id) if row.causation_id else None,
        workspace_id=WorkspaceId(row.workspace_id),
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        actor=row.actor,
        kind=kind,
        status=status,
        name=row.name,
        reason_code=row.reason_code,
        started_at=_aware(row.started_at),
        ended_at=_aware(row.ended_at) if row.ended_at else None,
        duration_ms=row.duration_ms,
        attributes=row.attributes or {},
        sequence=row.sequence,
    )


class SqlTraceRepository:
    """A fail-open sink and source-backed trace reader."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        maximum_events_per_workspace: int = 10_000,
        retention_days: int = 30,
    ) -> None:
        self._session_factory = session_factory
        self._maximum = max(1, maximum_events_per_workspace)
        self._retention = timedelta(days=max(1, retention_days))
        self._last_error = ""
        self._dropped = 0

    async def emit(self, event: TraceEvent) -> None:
        safe = event.sanitized()
        try:
            async with session_scope(self._session_factory) as session:
                row = TraceEventRow(
                    event_id=str(safe.event_id),
                    schema_version=safe.schema_version,
                    trace_id=str(safe.trace_id),
                    span_id=str(safe.span_id),
                    parent_id=str(safe.parent_id) if safe.parent_id else None,
                    correlation_id=safe.correlation_id,
                    causation_id=str(safe.causation_id) if safe.causation_id else None,
                    workspace_id=str(safe.workspace_id),
                    entity_type=safe.entity_type,
                    entity_id=safe.entity_id,
                    actor=safe.actor,
                    kind=safe.kind.value,
                    status=safe.status.value,
                    name=safe.name,
                    reason_code=safe.reason_code,
                    started_at=safe.started_at,
                    ended_at=safe.ended_at,
                    duration_ms=safe.duration_ms,
                    attributes=safe.attributes,
                )
                session.add(row)
                await session.flush()
                await session.execute(
                    delete(TraceEventRow).where(
                        TraceEventRow.workspace_id == str(safe.workspace_id),
                        TraceEventRow.started_at < datetime.now(UTC) - self._retention,
                    )
                )
                count = await session.scalar(
                    select(func.count(TraceEventRow.sequence)).where(
                        TraceEventRow.workspace_id == str(safe.workspace_id)
                    )
                )
                overflow = max(0, int(count or 0) - self._maximum)
                if overflow:
                    oldest = list(
                        await session.scalars(
                            select(TraceEventRow.sequence)
                            .where(TraceEventRow.workspace_id == str(safe.workspace_id))
                            .order_by(TraceEventRow.sequence)
                            .limit(overflow)
                        )
                    )
                    await session.execute(
                        delete(TraceEventRow).where(TraceEventRow.sequence.in_(oldest))
                    )
            self._last_error = ""
        except Exception as error:  # observability cannot change work's result
            self._dropped += 1
            self._last_error = str(redact(f"{type(error).__name__}: {error}"))
            log.warning("trace.not_recorded", error=self._last_error)

    def health(self) -> TraceHealth:
        return TraceHealth(
            available=not self._last_error,
            dropped=self._dropped,
            last_error=self._last_error,
        )

    async def prune(self, workspace_id: WorkspaceId, before: datetime) -> int:
        try:
            async with session_scope(self._session_factory) as session:
                result = await session.execute(
                    delete(TraceEventRow).where(
                        TraceEventRow.workspace_id == str(workspace_id),
                        TraceEventRow.started_at < before,
                    )
                )
                return int(result.rowcount or 0)
        except SQLAlchemyError as error:
            self._last_error = str(redact(error))
            return 0

    async def recent(
        self,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        *,
        limit: int = 50,
        entity_type: str = "",
        entity_id: str = "",
    ) -> list[TraceView]:
        async with session_scope(self._session_factory) as session:
            statement = (
                select(TraceEventRow.trace_id, func.max(TraceEventRow.started_at))
                .where(TraceEventRow.workspace_id == str(workspace_id))
                .group_by(TraceEventRow.trace_id)
                .order_by(func.max(TraceEventRow.started_at).desc())
                .limit(limit)
            )
            if entity_type:
                statement = statement.where(TraceEventRow.entity_type == entity_type)
            if entity_id:
                statement = statement.where(TraceEventRow.entity_id == entity_id)
            ids = [row[0] for row in (await session.execute(statement)).all()]
            if not ids and not entity_type and not entity_id:
                ids = list(
                    await session.scalars(
                        select(ObjectiveRow.id)
                        .where(ObjectiveRow.workspace_id == str(workspace_id))
                        .order_by(ObjectiveRow.created_at.desc())
                        .limit(limit)
                    )
                )
        found = [await self.get(UUID(item)) for item in ids]
        return [item for item in found if item is not None]

    async def get(self, identifier: UUID) -> TraceView | None:
        async with session_scope(self._session_factory) as session:
            trace_id, root_type, root_id, run_kind = await self._resolve(session, identifier)
            if trace_id is None:
                return None
            stored = list(
                await session.scalars(
                    select(TraceEventRow)
                    .where(TraceEventRow.trace_id == str(trace_id))
                    .order_by(TraceEventRow.sequence)
                )
            )
            events = [_row_event(row) for row in stored]
            projected, workspace = await self._project(
                session, trace_id, root_type, root_id, run_kind
            )
        combined = [*projected, *events]
        combined.sort(key=lambda event: (event.started_at, event.sequence, str(event.event_id)))
        combined = [replace(event, sequence=index) for index, event in enumerate(combined, 1)]
        return TraceView(
            trace_id=trace_id,
            run_kind=run_kind,
            workspace_id=workspace,
            root_entity_type=root_type,
            root_entity_id=root_id,
            events=tuple(combined),
            degraded=bool(self._last_error),
            degradation_reason=self._last_error,
        )

    async def _resolve(
        self, session: AsyncSession, identifier: UUID
    ) -> tuple[UUID | None, str, str, RunKind]:
        raw = str(identifier)
        objective = await session.get(ObjectiveRow, raw)
        if objective is not None:
            scheduled = await session.scalar(
                select(ScheduleRow.id).where(ScheduleRow.last_objective_id == raw).limit(1)
            )
            if scheduled:
                return identifier, "objective", raw, RunKind.SCHEDULE
            planned = await session.scalar(
                select(PlanRow.id).where(PlanRow.objective_id == raw).limit(1)
            )
            return identifier, "objective", raw, RunKind.TASK if planned else RunKind.ASK
        workflow = await session.get(WorkflowRunRow, raw)
        if workflow is not None:
            kind = RunKind.SCHEDULE if workflow.trigger != "MANUAL" else RunKind.WORKFLOW
            return identifier, "workflow_run", raw, kind
        task = await session.get(TaskRow, raw)
        if task is not None:
            if task.plan_id:
                plan = await session.get(PlanRow, task.plan_id)
                if plan is not None:
                    return UUID(plan.objective_id), "objective", plan.objective_id, RunKind.TASK
            if task.workflow_run_id:
                return (
                    UUID(task.workflow_run_id),
                    "workflow_run",
                    task.workflow_run_id,
                    RunKind.WORKFLOW,
                )
            return identifier, "task", raw, RunKind.TASK
        event_trace = await session.scalar(
            select(TraceEventRow.trace_id).where(
                or_(
                    TraceEventRow.trace_id == raw,
                    TraceEventRow.entity_id == raw,
                    TraceEventRow.span_id == raw,
                )
            )
        )
        return (
            (UUID(event_trace), "trace", event_trace, RunKind.TASK)
            if event_trace
            else (None, "", "", RunKind.TASK)
        )

    async def _project(
        self,
        session: AsyncSession,
        trace_id: UUID,
        root_type: str,
        root_id: str,
        run_kind: RunKind,
    ) -> tuple[list[TraceEvent], WorkspaceId]:
        if root_type == "objective":
            return await self._objective(session, trace_id)
        if root_type == "workflow_run":
            return await self._workflow(session, trace_id)
        if root_type == "task":
            task = await session.get(TaskRow, root_id)
            if task is None:
                return [], DEFAULT_WORKSPACE_ID
            workspace = WorkspaceId(task.workspace_id)
            root = self._task_event(trace_id, task, None)
            children = await self._task_children(session, trace_id, task, root.span_id)
            return [root, *children], workspace
        rows = list(
            await session.scalars(
                select(TraceEventRow).where(TraceEventRow.trace_id == str(trace_id))
            )
        )
        workspace = WorkspaceId(rows[0].workspace_id) if rows else DEFAULT_WORKSPACE_ID
        return [], workspace

    async def _objective(
        self, session: AsyncSession, trace_id: UUID
    ) -> tuple[list[TraceEvent], WorkspaceId]:
        objective = await session.get(ObjectiveRow, str(trace_id))
        if objective is None:
            return [], DEFAULT_WORKSPACE_ID
        workspace = WorkspaceId(objective.workspace_id)
        schedule = await session.scalar(
            select(ScheduleRow).where(ScheduleRow.last_objective_id == objective.id).limit(1)
        )
        schedule_span = (
            stable_span_id(trace_id, SpanKind.SCHEDULE, schedule.id) if schedule else None
        )
        objective_span = stable_span_id(trace_id, SpanKind.OBJECTIVE, objective.id)
        events: list[TraceEvent] = []
        if schedule:
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=schedule_span,
                    kind=SpanKind.SCHEDULE,
                    status=_status(objective.status),
                    name="Schedule occurrence",
                    workspace_id=workspace,
                    entity_type="schedule",
                    entity_id=schedule.id,
                    actor="scheduler",
                    reason_code="OCCURRENCE",
                    started_at=_aware(schedule.last_run_at or objective.created_at),
                    ended_at=_aware(objective.finished_at) if objective.finished_at else None,
                    attributes={
                        "name": schedule.name,
                        "timezone": schedule.timezone,
                        "request": "not captured",
                    },
                )
            )
        events.append(
            TraceEvent(
                trace_id=trace_id,
                span_id=objective_span,
                parent_id=schedule_span,
                causation_id=schedule_span,
                kind=SpanKind.OBJECTIVE,
                status=_status(objective.status),
                name="User request",
                workspace_id=workspace,
                entity_type="objective",
                entity_id=objective.id,
                actor="user",
                started_at=_aware(objective.created_at),
                ended_at=_aware(objective.finished_at) if objective.finished_at else None,
                duration_ms=(
                    int((objective.finished_at - objective.created_at).total_seconds() * 1000)
                    if objective.finished_at
                    else None
                ),
                attributes={
                    "status": objective.status,
                    "acceptance_criteria_count": len(objective.acceptance_criteria or []),
                    "content": "not captured",
                },
            )
        )
        plans = list(
            await session.scalars(
                select(PlanRow)
                .where(PlanRow.objective_id == objective.id)
                .order_by(PlanRow.revision)
            )
        )
        for plan in plans:
            plan_span = stable_span_id(trace_id, SpanKind.PLAN, plan.id)
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=plan_span,
                    parent_id=objective_span,
                    causation_id=objective_span,
                    kind=SpanKind.REPLAN if plan.revision > 1 else SpanKind.PLAN,
                    status=_status(plan.status),
                    name=f"Plan revision {plan.revision}",
                    workspace_id=workspace,
                    entity_type="plan",
                    entity_id=plan.id,
                    actor="prometheus",
                    reason_code="REPLAN" if plan.revision > 1 else "PLANNED",
                    started_at=_aware(plan.created_at),
                    attributes={"revision": plan.revision, "status": plan.status},
                )
            )
            tasks = list(
                await session.scalars(
                    select(TaskRow)
                    .where(TaskRow.plan_id == plan.id)
                    .order_by(TaskRow.created_at, TaskRow.id)
                )
            )
            for task in tasks:
                task_event = self._task_event(trace_id, task, plan_span)
                events.append(task_event)
                events.extend(
                    await self._task_children(session, trace_id, task, task_event.span_id)
                )
        return events, workspace

    async def _workflow(
        self, session: AsyncSession, trace_id: UUID
    ) -> tuple[list[TraceEvent], WorkspaceId]:
        run = await session.get(WorkflowRunRow, str(trace_id))
        if run is None:
            return [], DEFAULT_WORKSPACE_ID
        workspace = WorkspaceId(run.workspace_id)
        root = TraceEvent(
            trace_id=trace_id,
            span_id=stable_span_id(trace_id, SpanKind.WORKFLOW, run.id),
            kind=SpanKind.WORKFLOW,
            status=_status(run.status),
            name=f"Workflow {run.workflow}",
            workspace_id=workspace,
            entity_type="workflow_run",
            entity_id=run.id,
            actor="prometheus",
            reason_code=run.trigger,
            started_at=_aware(run.started_at),
            ended_at=_aware(run.finished_at) if run.finished_at else None,
            attributes={
                "workflow": run.workflow,
                "version": run.workflow_version,
                "trigger": run.trigger,
                "inputs": "not captured",
            },
        )
        tasks = list(
            await session.scalars(
                select(TaskRow)
                .where(TaskRow.workflow_run_id == run.id)
                .order_by(TaskRow.created_at, TaskRow.id)
            )
        )
        events = [root]
        for task in tasks:
            task_event = self._task_event(trace_id, task, root.span_id)
            events.append(task_event)
            events.extend(await self._task_children(session, trace_id, task, task_event.span_id))
        return events, workspace

    def _task_event(self, trace_id: UUID, task: TaskRow, parent_id: UUID | None) -> TraceEvent:
        return TraceEvent(
            trace_id=trace_id,
            span_id=stable_span_id(trace_id, SpanKind.TASK, task.id),
            parent_id=parent_id,
            causation_id=parent_id,
            kind=SpanKind.TASK,
            status=_status(task.status),
            name="Task execution",
            workspace_id=WorkspaceId(task.workspace_id),
            entity_type="task",
            entity_id=task.id,
            actor=str(task.assigned_employee_id or "prometheus"),
            reason_code=task.error.get("kind", "") if task.error else "",
            started_at=_aware(task.created_at),
            ended_at=(
                _aware(task.updated_at)
                if task.status in {"COMPLETED", "FAILED", "CANCELLED"}
                else None
            ),
            duration_ms=int((task.updated_at - task.created_at).total_seconds() * 1000),
            attributes={
                "status": task.status,
                "attempts": task.attempts,
                "cost_usd": task.cost_usd,
                "evidence_count": len((task.result or {}).get("artifacts", [])),
                "goal": "not captured",
            },
        )

    async def _task_children(
        self, session: AsyncSession, trace_id: UUID, task: TaskRow, parent: UUID
    ) -> list[TraceEvent]:
        workspace = WorkspaceId(task.workspace_id)
        events: list[TraceEvent] = []
        assignments = list(
            await session.scalars(
                select(TaskAssignmentRow)
                .where(TaskAssignmentRow.task_id == task.id)
                .order_by(TaskAssignmentRow.assigned_at, TaskAssignmentRow.id)
            )
        )
        for index, assignment in enumerate(assignments):
            decision = assignment.decision or {}
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=stable_span_id(trace_id, SpanKind.ASSIGNMENT, assignment.id),
                    parent_id=parent,
                    causation_id=parent,
                    kind=SpanKind.REASSIGNMENT if index else SpanKind.ASSIGNMENT,
                    status=_status(assignment.outcome or "RUNNING"),
                    name="Employee assignment",
                    workspace_id=workspace,
                    entity_type="assignment",
                    entity_id=assignment.id,
                    actor=str(assignment.employee_id),
                    reason_code=str(decision.get("code", "UNRECORDED")),
                    started_at=_aware(assignment.assigned_at),
                    ended_at=_aware(assignment.completed_at) if assignment.completed_at else None,
                    attributes={
                        "assigned_by": assignment.assigned_by,
                        "employee_id": assignment.employee_id,
                        "accepted": (assignment.acceptance or {}).get("accepted"),
                        "selection_reason": decision.get("reason", "unrecorded"),
                    },
                )
            )
        transitions = list(
            await session.scalars(
                select(TaskEventRow)
                .where(TaskEventRow.task_id == task.id)
                .order_by(TaskEventRow.id)
            )
        )
        for transition in transitions:
            kind = (
                SpanKind.RETRY
                if transition.to_status == "PLANNING" and transition.from_status
                else SpanKind.STATE
            )
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=stable_span_id(trace_id, kind, f"{task.id}:{transition.id}"),
                    parent_id=parent,
                    causation_id=parent,
                    kind=kind,
                    status=_status(transition.to_status),
                    name="Task state changed",
                    workspace_id=workspace,
                    entity_type="task_event",
                    entity_id=str(transition.id),
                    actor=str(task.assigned_employee_id or "prometheus"),
                    reason_code=transition.to_status,
                    started_at=_aware(transition.created_at),
                    attributes={
                        "from": transition.from_status or "CREATED",
                        "to": transition.to_status,
                    },
                )
            )
        models = list(
            await session.scalars(
                select(LLMCallRow)
                .where(LLMCallRow.task_id == task.id)
                .order_by(LLMCallRow.created_at, LLMCallRow.id)
            )
        )
        for model in models:
            model_parent = parent
            if model.escalation_level:
                escalation_span = stable_span_id(trace_id, SpanKind.ESCALATION, str(model.id))
                events.append(
                    TraceEvent(
                        trace_id=trace_id,
                        span_id=escalation_span,
                        parent_id=parent,
                        causation_id=parent,
                        kind=SpanKind.ESCALATION,
                        status=SpanStatus.OK if model.success else SpanStatus.ERROR,
                        name="Model escalation",
                        workspace_id=workspace,
                        entity_type="llm_call",
                        entity_id=str(model.id),
                        actor=model.entry or "router",
                        reason_code="ESCALATED",
                        started_at=_aware(model.created_at),
                        attributes={"level": model.escalation_level, "reason": model.reason},
                    )
                )
                model_parent = escalation_span
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=stable_span_id(trace_id, SpanKind.MODEL, str(model.id)),
                    parent_id=model_parent,
                    causation_id=model_parent,
                    kind=SpanKind.MODEL,
                    status=SpanStatus.OK if model.success else SpanStatus.ERROR,
                    name="Model call",
                    workspace_id=workspace,
                    entity_type="llm_call",
                    entity_id=str(model.id),
                    actor=model.entry or model.model,
                    reason_code="ESCALATED" if model.escalation_level else "ROUTED",
                    started_at=_aware(model.created_at),
                    ended_at=_aware(model.created_at),
                    duration_ms=model.latency_ms,
                    attributes={
                        "provider": model.provider,
                        "model": model.model,
                        "routing_reason": model.reason,
                        "escalation_level": model.escalation_level,
                        "cost_usd": model.cost_usd,
                        "prompt": "not captured",
                        "response": "not captured",
                    },
                )
            )
        for index, artifact in enumerate((task.result or {}).get("artifacts", []), start=1):
            fingerprint = hashlib.sha256(str(artifact).encode()).hexdigest()[:16]
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=stable_span_id(trace_id, SpanKind.ARTIFACT, f"{task.id}:{fingerprint}"),
                    parent_id=parent,
                    causation_id=parent,
                    kind=SpanKind.ARTIFACT,
                    status=SpanStatus.OK,
                    name=f"Artifact {index}",
                    workspace_id=workspace,
                    entity_type="artifact",
                    entity_id=fingerprint,
                    actor=str(task.assigned_employee_id or "prometheus"),
                    reason_code="EVIDENCE_RECORDED",
                    started_at=_aware(task.updated_at),
                    ended_at=_aware(task.updated_at),
                    attributes={"content": "not captured", "path": "not captured"},
                )
            )
        tools = list(
            await session.scalars(
                select(ToolCallRow)
                .where(ToolCallRow.task_id == task.id)
                .order_by(ToolCallRow.created_at, ToolCallRow.id)
            )
        )
        for tool in tools:
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=stable_span_id(trace_id, SpanKind.TOOL, str(tool.id)),
                    parent_id=parent,
                    causation_id=parent,
                    kind=SpanKind.TOOL,
                    status=SpanStatus.OK if tool.success else SpanStatus.ERROR,
                    name=f"Tool {tool.tool}",
                    workspace_id=workspace,
                    entity_type="tool_call",
                    entity_id=str(tool.id),
                    actor=str(task.assigned_employee_id or ""),
                    reason_code="COMPLETED" if tool.completed else "INTERRUPTED",
                    started_at=_aware(tool.created_at),
                    ended_at=_aware(tool.created_at),
                    duration_ms=tool.latency_ms,
                    attributes={
                        "tool": tool.tool,
                        "interface": tool.interface,
                        "input": "redacted",
                        "output": "redacted",
                    },
                )
            )
        approvals = list(
            await session.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.task_id == task.id)
                .order_by(ApprovalRow.requested_at, ApprovalRow.id)
            )
        )
        for approval in approvals:
            duration = None
            if approval.resolved_at:
                duration = int(
                    (approval.resolved_at - approval.requested_at).total_seconds() * 1000
                )
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=stable_span_id(trace_id, SpanKind.APPROVAL, approval.id),
                    parent_id=parent,
                    causation_id=parent,
                    kind=SpanKind.APPROVAL,
                    status=_status(approval.state),
                    name="Approval decision",
                    workspace_id=workspace,
                    entity_type="approval",
                    entity_id=approval.id,
                    actor=approval.resolved_by or "user",
                    reason_code=approval.state,
                    started_at=_aware(approval.requested_at),
                    ended_at=_aware(approval.resolved_at) if approval.resolved_at else None,
                    duration_ms=duration,
                    attributes={
                        "tool": approval.tool,
                        "risk": approval.risk_level,
                        "grant": approval.grant_kind,
                        "lease_id": approval.lease_id or "",
                        "payload": "not captured",
                    },
                )
            )
        audits = list(
            await session.scalars(
                select(AuditRow)
                .where(AuditRow.task_id == task.id)
                .order_by(AuditRow.ts, AuditRow.id)
            )
        )
        approved_tools = {
            audit.tool
            for audit in audits
            if (audit.details or {}).get("decision") == "APPROVED" and audit.result == "SUCCESS"
        }
        irreversible = {"DELETE", "SEND", "PUBLISH", "SPEND"}
        for audit in audits:
            details = audit.details or {}
            effect = details.get("effect", "")
            decision = details.get("decision", "")
            unsafe = bool(
                not decision
                and audit.result == "SUCCESS"
                and effect in irreversible
                and audit.tool not in approved_tools
            )
            events.append(
                TraceEvent(
                    trace_id=trace_id,
                    span_id=stable_span_id(trace_id, SpanKind.APPROVAL, f"audit:{audit.id}"),
                    parent_id=parent,
                    causation_id=parent,
                    kind=SpanKind.APPROVAL,
                    status=_status(audit.result),
                    name="Policy decision" if decision else "Action audit",
                    workspace_id=workspace,
                    entity_type="audit",
                    entity_id=str(audit.id),
                    actor=audit.actor_id or audit.actor_kind,
                    reason_code=str(details.get("policy_source", "")),
                    started_at=_aware(audit.ts),
                    attributes={
                        "effect": effect,
                        "tool": audit.tool or "",
                        "decision": decision or "not recorded",
                        "unsafe_without_approval": unsafe,
                    },
                )
            )
        return events


class InMemoryTraceRepository:
    def __init__(self, *, maximum_events: int = 1_000) -> None:
        self.events: list[TraceEvent] = []
        self._maximum = maximum_events
        self._dropped = 0

    async def emit(self, event: TraceEvent) -> None:
        if len(self.events) >= self._maximum:
            self.events.pop(0)
            self._dropped += 1
        self.events.append(event.sanitized())

    def health(self) -> TraceHealth:
        return TraceHealth(True, dropped=self._dropped)

    async def prune(self, workspace_id: WorkspaceId, before: datetime) -> int:
        original = len(self.events)
        self.events = [
            event
            for event in self.events
            if event.workspace_id != workspace_id or event.started_at >= before
        ]
        return original - len(self.events)

    async def get(self, identifier: UUID) -> TraceView | None:
        matching = [
            event
            for event in self.events
            if event.trace_id == identifier
            or event.span_id == identifier
            or event.entity_id == str(identifier)
        ]
        if not matching:
            return None
        trace_id = matching[0].trace_id
        matching = sorted(
            (event for event in self.events if event.trace_id == trace_id),
            key=lambda event: (event.started_at, event.sequence, str(event.event_id)),
        )
        root = matching[0]
        return TraceView(
            trace_id=trace_id,
            run_kind=RunKind.TASK,
            workspace_id=root.workspace_id,
            root_entity_type=root.entity_type,
            root_entity_id=root.entity_id,
            events=tuple(replace(event, sequence=index) for index, event in enumerate(matching, 1)),
        )

    async def recent(
        self,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        *,
        limit: int = 50,
        entity_type: str = "",
        entity_id: str = "",
    ) -> list[TraceView]:
        ids: list[UUID] = []
        for event in reversed(self.events):
            if event.workspace_id != workspace_id:
                continue
            if entity_type and event.entity_type != entity_type:
                continue
            if entity_id and event.entity_id != entity_id:
                continue
            if event.trace_id not in ids:
                ids.append(event.trace_id)
        views = [await self.get(trace_id) for trace_id in ids[:limit]]
        return [view for view in views if view is not None]
