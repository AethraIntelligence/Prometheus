"""Translation between domain values and ORM rows.

Kept apart from the repositories so the domain never learns the row shape and
the rows never grow behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from domain.tasks.plan import TaskPlan
from domain.tasks.task import (
    Execution,
    Task,
    TaskCreatedBy,
    TaskError,
    TaskEvent,
    TaskResult,
    TaskStatus,
)
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.models import TaskEventRow, TaskRow


def _as_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


def _as_aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; the domain works in UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def task_to_row(task: Task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "workspace_id": str(task.workspace_id),
        "created_by": task.created_by.value,
        "parent_id": str(task.parent_id) if task.parent_id else None,
        "plan_id": str(task.plan_id) if task.plan_id else None,
        "workflow_run_id": str(task.workflow_run_id) if task.workflow_run_id else None,
        "goal": task.goal,
        "status": task.status.value,
        "priority": task.priority,
        "assigned_employee_id": (
            str(task.assigned_employee_id) if task.assigned_employee_id else None
        ),
        "assignment_reason": task.assignment_reason,
        "plan": task.plan.to_dict() if task.plan else None,
        "state": {"step": task.execution.step, "data": task.execution.state},
        "result": (
            {
                "summary": task.result.summary,
                "output": task.result.output,
                "artifacts": list(task.result.artifacts),
            }
            if task.result
            else None
        ),
        "error": (
            {"kind": task.error.kind, "message": task.error.message, "details": task.error.details}
            if task.error
            else None
        ),
        "attempts": task.attempts,
        "cost_usd": task.cost_usd,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def row_to_task(row: TaskRow) -> Task:
    state = row.state or {}
    result = row.result
    error = row.error
    return Task(
        id=UUID(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        goal=row.goal,
        status=TaskStatus(row.status),
        created_by=TaskCreatedBy(row.created_by),
        priority=row.priority,
        parent_id=_as_uuid(row.parent_id),
        plan_id=_as_uuid(row.plan_id),
        workflow_run_id=_as_uuid(row.workflow_run_id),
        assigned_employee_id=_as_uuid(row.assigned_employee_id),
        assignment_reason=row.assignment_reason or "",
        plan=TaskPlan.from_dict(row.plan),
        execution=Execution(step=state.get("step", 0), state=state.get("data", {})),
        result=(
            TaskResult(
                summary=result["summary"],
                output=result.get("output", {}),
                artifacts=tuple(result.get("artifacts", ())),
            )
            if result
            else None
        ),
        error=(
            TaskError(
                kind=error["kind"], message=error["message"], details=error.get("details", {})
            )
            if error
            else None
        ),
        attempts=row.attempts,
        cost_usd=row.cost_usd,
        created_at=_as_aware(row.created_at),
        updated_at=_as_aware(row.updated_at),
    )


def event_to_row(event: TaskEvent) -> TaskEventRow:
    return TaskEventRow(
        task_id=str(event.task_id),
        from_status=event.from_status.value if event.from_status else None,
        to_status=event.to_status.value,
        payload=event.payload or None,
        created_at=event.created_at,
    )


def row_to_event(row: TaskEventRow) -> TaskEvent:
    return TaskEvent(
        task_id=UUID(row.task_id),
        from_status=TaskStatus(row.from_status) if row.from_status else None,
        to_status=TaskStatus(row.to_status),
        payload=row.payload or {},
        created_at=_as_aware(row.created_at),
    )
