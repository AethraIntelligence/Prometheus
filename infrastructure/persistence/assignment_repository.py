"""Assignment storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.employees.definition import EmployeeId
from domain.errors import StorageError, StorageNotInitializedError
from domain.policies.models import ActorKind
from domain.tasks.task import TaskResult
from domain.workforce.acceptance import Acceptance
from domain.workforce.assignment import AssignmentOutcome, SharedContext, TaskAssignment
from domain.workforce.decision import Decision
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import TaskAssignmentRow
from infrastructure.persistence.session import session_scope


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _to_values(assignment: TaskAssignment) -> dict:
    return {
        "id": str(assignment.id),
        "workspace_id": str(assignment.workspace_id),
        "task_id": str(assignment.task_id),
        "employee_id": str(assignment.employee_id),
        "assigned_by": assignment.assigned_by.value,
        "assigned_by_id": assignment.assigned_by_id,
        "context": {
            "facts": list(assignment.context.facts),
            "constraints": list(assignment.context.constraints),
            "artifacts": list(assignment.context.artifacts),
            "data": assignment.context.data,
        },
        "assigned_at": assignment.assigned_at,
        "accepted_at": assignment.accepted_at,
        "completed_at": assignment.completed_at,
        "outcome": assignment.outcome.value if assignment.outcome else None,
        "result": (
            {
                "summary": assignment.result.summary,
                "output": assignment.result.output,
                "artifacts": list(assignment.result.artifacts),
            }
            if assignment.result
            else None
        ),
        "decision": assignment.decision.to_dict() if assignment.decision else {},
        "acceptance": assignment.acceptance.to_dict() if assignment.acceptance else None,
    }


def _to_assignment(row: TaskAssignmentRow) -> TaskAssignment:
    context = row.context or {}
    result = row.result
    return TaskAssignment(
        id=UUID(row.id),
        task_id=UUID(row.task_id),
        employee_id=UUID(row.employee_id),
        assigned_by=ActorKind(row.assigned_by),
        context=SharedContext(
            facts=tuple(context.get("facts", ())),
            constraints=tuple(context.get("constraints", ())),
            artifacts=tuple(context.get("artifacts", ())),
            data=context.get("data", {}),
        ),
        workspace_id=WorkspaceId(row.workspace_id),
        assigned_by_id=row.assigned_by_id,
        assigned_at=_aware(row.assigned_at),  # type: ignore[arg-type]
        accepted_at=_aware(row.accepted_at),
        completed_at=_aware(row.completed_at),
        outcome=AssignmentOutcome(row.outcome) if row.outcome else None,
        result=(
            TaskResult(
                summary=result["summary"],
                output=result.get("output", {}),
                artifacts=tuple(result.get("artifacts", ())),
            )
            if result
            else None
        ),
        # Both read leniently: an old row has neither, and a row written by a
        # newer build in a format this one does not know is unrecorded here
        # rather than misread.
        decision=Decision.from_dict(row.decision),
        acceptance=Acceptance.from_dict(row.acceptance),
    )


class SqlAssignmentRepository:
    """Implements `domain.workforce.repository.AssignmentRepository`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        try:
            async with session_scope(self._session_factory) as session:
                yield session
        except OperationalError as error:
            message = str(error.orig)
            if "no such table" in message or "unable to open database file" in message:
                raise StorageNotInitializedError("The local database has no schema yet.") from error
            raise StorageError(message) from error

    async def save(self, assignment: TaskAssignment) -> None:
        values = _to_values(assignment)
        async with self._session() as session:
            statement = upsert(session, TaskAssignmentRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[TaskAssignmentRow.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "assigned_at")},
                )
            )

    async def get(self, assignment_id: UUID) -> TaskAssignment | None:
        async with self._session() as session:
            row = await session.get(TaskAssignmentRow, str(assignment_id))
            return _to_assignment(row) if row else None

    async def for_task(self, task_id: UUID) -> list[TaskAssignment]:
        async with self._session() as session:
            rows = await session.scalars(
                select(TaskAssignmentRow)
                .where(TaskAssignmentRow.task_id == str(task_id))
                .order_by(TaskAssignmentRow.assigned_at.desc())
            )
            return [_to_assignment(row) for row in rows]

    async def for_employee(
        self,
        employee_id: EmployeeId,
        *,
        workspace_id: WorkspaceId | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TaskAssignment]:
        statement = select(TaskAssignmentRow).where(
            TaskAssignmentRow.employee_id == str(employee_id)
        )
        if since is not None:
            statement = statement.where(TaskAssignmentRow.assigned_at >= since)
        if workspace_id is not None:
            statement = statement.where(TaskAssignmentRow.workspace_id == str(workspace_id))
        statement = statement.order_by(TaskAssignmentRow.assigned_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        async with self._session() as session:
            rows = await session.scalars(statement)
            return [_to_assignment(row) for row in rows]


class InMemoryAssignmentRepository:
    """Implements `domain.workforce.repository.AssignmentRepository`."""

    def __init__(self) -> None:
        self._assignments: dict[UUID, TaskAssignment] = {}

    async def save(self, assignment: TaskAssignment) -> None:
        self._assignments[assignment.id] = assignment

    async def get(self, assignment_id: UUID) -> TaskAssignment | None:
        return self._assignments.get(assignment_id)

    async def for_task(self, task_id: UUID) -> list[TaskAssignment]:
        return sorted(
            (a for a in self._assignments.values() if a.task_id == task_id),
            key=lambda a: a.assigned_at,
            reverse=True,
        )

    async def for_employee(
        self,
        employee_id: EmployeeId,
        *,
        workspace_id: WorkspaceId | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TaskAssignment]:
        found = sorted(
            (
                a
                for a in self._assignments.values()
                if a.employee_id == employee_id
                and (workspace_id is None or a.workspace_id == workspace_id)
                and (since is None or a.assigned_at >= since)
            ),
            key=lambda a: a.assigned_at,
            reverse=True,
        )
        return found[:limit] if limit is not None else found
