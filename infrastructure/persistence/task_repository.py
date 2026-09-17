"""SQLite-backed task storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.tasks.task import RESUMABLE_STATUSES, Task, TaskEvent
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.mappers import event_to_row, row_to_event, row_to_task, task_to_row
from infrastructure.persistence.models import TaskEventRow, TaskRow
from infrastructure.persistence.session import session_scope


class SqlTaskRepository:
    """Implements `domain.tasks.repository.TaskRepository`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """Translate storage failures into the domain taxonomy at the boundary.

        Nothing above `infrastructure/` should have to catch a SQLAlchemy error.
        """
        try:
            async with session_scope(self._session_factory) as session:
                yield session
        except OperationalError as error:
            message = str(error.orig)
            if "no such table" in message or "unable to open database file" in message:
                raise StorageNotInitializedError(
                    "The local database has no schema yet."
                ) from error
            raise StorageError(message) from error

    async def save(self, task: Task, event: TaskEvent | None = None) -> None:
        values = task_to_row(task)
        async with self._session() as session:
            # Upsert: the runtime saves after every step, and a resumed task is
            # written back under the id it already has.
            statement = upsert(session, TaskRow).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[TaskRow.id],
                set_={k: v for k, v in values.items() if k not in ("id", "created_at")},
            )
            await session.execute(statement)
            if event is not None:
                session.add(event_to_row(event))

    async def get(self, task_id: UUID) -> Task | None:
        async with self._session() as session:
            row = await session.get(TaskRow, str(task_id))
            return row_to_task(row) if row else None

    async def list_resumable(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Task]:
        statuses = [status.value for status in RESUMABLE_STATUSES]
        async with self._session() as session:
            rows = await session.scalars(
                select(TaskRow)
                .where(TaskRow.workspace_id == str(workspace_id))
                .where(TaskRow.status.in_(statuses))
                .order_by(TaskRow.priority.desc(), TaskRow.created_at)
            )
            return [row_to_task(row) for row in rows]

    async def list_in_status(
        self, status, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Task]:
        async with self._session() as session:
            rows = await session.scalars(
                select(TaskRow)
                .where(TaskRow.workspace_id == str(workspace_id))
                .where(TaskRow.status == status.value)
                .order_by(TaskRow.created_at)
            )
            return [row_to_task(row) for row in rows]

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Task]:
        async with self._session() as session:
            rows = await session.scalars(
                select(TaskRow)
                .where(TaskRow.workspace_id == str(workspace_id))
                .order_by(TaskRow.created_at.desc(), TaskRow.id.desc())
                .limit(limit)
            )
            return [row_to_task(row) for row in rows]

    async def events(self, task_id: UUID) -> list[TaskEvent]:
        async with self._session() as session:
            rows = await session.scalars(
                select(TaskEventRow)
                .where(TaskEventRow.task_id == str(task_id))
                .order_by(TaskEventRow.id)
            )
            return [row_to_event(row) for row in rows]
