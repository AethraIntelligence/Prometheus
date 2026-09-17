"""Which memories work was given. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.memory.usage import MemoryUse
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.models import MemoryUseRow
from infrastructure.persistence.session import session_scope


def _to_use(row: MemoryUseRow) -> MemoryUse:
    return MemoryUse(
        id=UUID(row.id),
        memory_id=UUID(row.memory_id),
        reason=row.reason,
        weight=row.weight,
        objective_id=UUID(row.objective_id) if row.objective_id else None,
        task_id=UUID(row.task_id) if row.task_id else None,
        workspace_id=WorkspaceId(row.workspace_id),
        reader=row.reader,
        used_at=_aware(row.used_at),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SqlMemoryUseLog:
    """Implements `domain.memory.usage.MemoryUseLog`."""

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
                raise StorageNotInitializedError(
                    "The local database has no schema yet."
                ) from error
            raise StorageError(message) from error

    async def record(self, uses: list[MemoryUse]) -> None:
        if not uses:
            return
        async with self._session() as session:
            await session.execute(
                insert(MemoryUseRow),
                [
                    {
                        "id": str(use.id),
                        "workspace_id": str(use.workspace_id),
                        "memory_id": str(use.memory_id),
                        "objective_id": str(use.objective_id) if use.objective_id else None,
                        "task_id": str(use.task_id) if use.task_id else None,
                        "reader": use.reader[:32],
                        "reason": use.reason,
                        "weight": float(use.weight),
                        "used_at": use.used_at,
                    }
                    for use in uses
                ],
            )

    async def for_objective(self, objective_id: UUID) -> list[MemoryUse]:
        return await self._where(MemoryUseRow.objective_id == str(objective_id))

    async def for_task(self, task_id: UUID) -> list[MemoryUse]:
        return await self._where(MemoryUseRow.task_id == str(task_id))

    async def for_memory(self, memory_id: UUID, *, limit: int = 20) -> list[MemoryUse]:
        async with self._session() as session:
            rows = await session.scalars(
                select(MemoryUseRow)
                .where(MemoryUseRow.memory_id == str(memory_id))
                .order_by(MemoryUseRow.used_at.desc())
                .limit(limit)
            )
            return [_to_use(row) for row in rows]

    async def _where(self, condition) -> list[MemoryUse]:
        async with self._session() as session:
            rows = await session.scalars(
                select(MemoryUseRow).where(condition).order_by(MemoryUseRow.used_at)
            )
            return [_to_use(row) for row in rows]


class InMemoryMemoryUseLog:
    """Implements `domain.memory.usage.MemoryUseLog`."""

    def __init__(self) -> None:
        self._uses: list[MemoryUse] = []

    async def record(self, uses: list[MemoryUse]) -> None:
        self._uses.extend(deepcopy(uses))

    async def for_objective(self, objective_id: UUID) -> list[MemoryUse]:
        return [deepcopy(u) for u in self._uses if u.objective_id == objective_id]

    async def for_task(self, task_id: UUID) -> list[MemoryUse]:
        return [deepcopy(u) for u in self._uses if u.task_id == task_id]

    async def for_memory(self, memory_id: UUID, *, limit: int = 20) -> list[MemoryUse]:
        found = [u for u in self._uses if u.memory_id == memory_id]
        found.sort(key=lambda use: use.used_at, reverse=True)
        return deepcopy(found[:limit])
