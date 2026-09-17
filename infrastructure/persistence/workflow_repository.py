"""Workflow run storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.workflows.definition import WorkflowTrigger
from domain.workflows.run import RunStatus, StepOutcome, WorkflowRun
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import WorkflowRunRow
from infrastructure.persistence.session import session_scope


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _to_values(run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "workspace_id": str(run.workspace_id),
        "workflow": run.workflow,
        "workflow_version": run.workflow_version,
        "trigger": run.trigger.value,
        "input": dict(run.inputs),
        "status": run.status.value,
        "result": run.to_result(),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _to_run(row: WorkflowRunRow) -> WorkflowRun:
    result = row.result or {}
    steps = tuple(
        StepOutcome(
            step=str(entry.get("step", "")),
            employee=str(entry.get("employee", "")),
            task_id=UUID(entry["task_id"]) if entry.get("task_id") else None,
            succeeded=bool(entry.get("succeeded", False)),
            attempts=int(entry.get("attempts", 0)),
            summary=str(entry.get("summary", "")),
            cost_usd=float(entry.get("cost_usd", 0.0)),
        )
        for entry in result.get("steps", [])
    )
    return WorkflowRun(
        id=UUID(row.id),
        workflow=row.workflow,
        workflow_version=row.workflow_version or int(result.get("workflow_version", 1)),
        trigger=WorkflowTrigger(row.trigger),
        inputs=row.input or {},
        status=RunStatus(row.status),
        steps=steps,
        summary=str(result.get("summary", "")),
        workspace_id=WorkspaceId(row.workspace_id),
        started_at=_aware(row.started_at),  # type: ignore[arg-type]
        finished_at=_aware(row.finished_at),
    )


class SqlWorkflowRunRepository:
    """Implements `domain.workflows.run.WorkflowRunRepository`."""

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

    async def save(self, run: WorkflowRun) -> None:
        values = _to_values(run)
        async with self._session() as session:
            statement = upsert(session, WorkflowRunRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[WorkflowRunRow.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "started_at")},
                )
            )

    async def get(self, run_id: UUID) -> WorkflowRun | None:
        async with self._session() as session:
            row = await session.get(WorkflowRunRow, str(run_id))
            return _to_run(row) if row else None

    async def recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 20
    ) -> list[WorkflowRun]:
        async with self._session() as session:
            rows = await session.scalars(
                select(WorkflowRunRow)
                .where(WorkflowRunRow.workspace_id == str(workspace_id))
                .order_by(WorkflowRunRow.started_at.desc())
                .limit(limit)
            )
            return [_to_run(row) for row in rows]


class InMemoryWorkflowRunRepository:
    """Implements the same contract, for tests and for a run with no store."""

    def __init__(self) -> None:
        self._runs: dict[UUID, WorkflowRun] = {}

    async def save(self, run: WorkflowRun) -> None:
        self._runs[run.id] = run

    async def get(self, run_id: UUID) -> WorkflowRun | None:
        return self._runs.get(run_id)

    async def recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 20
    ) -> list[WorkflowRun]:
        matching = [run for run in self._runs.values() if run.workspace_id == workspace_id]
        return sorted(matching, key=lambda run: run.started_at, reverse=True)[:limit]
