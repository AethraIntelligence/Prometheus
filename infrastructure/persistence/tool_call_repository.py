"""Tool-call telemetry storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.computer.interfaces import InterfaceLevel
from domain.errors import StorageError, StorageNotInitializedError
from domain.tools.telemetry import ToolCallRecord
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ToolCallRow
from infrastructure.persistence.session import session_scope


def _to_record(row: ToolCallRow) -> ToolCallRecord:
    created = row.created_at
    return ToolCallRecord(
        tool=row.tool,
        success=row.success,
        latency_ms=row.latency_ms,
        task_id=UUID(row.task_id) if row.task_id else None,
        input_data=row.input or {},
        output=row.output or {},
        error=row.error,
        call_id=row.call_id,
        completed=row.completed,
        interface=_interface(row.interface),
        created_at=created if created.tzinfo else created.replace(tzinfo=UTC),
    )


def _interface(raw: str | None) -> InterfaceLevel:
    """A row written by an older version, or by a level since renamed, still reads."""
    try:
        return InterfaceLevel(raw or InterfaceLevel.API)
    except ValueError:
        return InterfaceLevel.API


class SqlToolCallLog:
    """Implements `domain.tools.telemetry.ToolCallLog`."""

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

    async def record(self, call: ToolCallRecord) -> None:
        safe = call.redacted()
        async with self._session() as session:
            session.add(
                ToolCallRow(
                    task_id=str(safe.task_id) if safe.task_id else None,
                    call_id=safe.call_id,
                    tool=safe.tool,
                    input=safe.input_data,
                    output=safe.output,
                    success=safe.success,
                    completed=safe.completed,
                    error=safe.error,
                    latency_ms=safe.latency_ms,
                    interface=safe.interface.value,
                    created_at=safe.created_at,
                )
            )

    async def list_for_task(self, task_id: UUID) -> list[ToolCallRecord]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ToolCallRow)
                .where(ToolCallRow.task_id == str(task_id))
                .order_by(ToolCallRow.id)
            )
            return [_to_record(row) for row in rows]

    async def get_call(self, task_id: UUID, call_id: str) -> ToolCallRecord | None:
        async with self._session() as session:
            row = await session.scalar(
                select(ToolCallRow).where(
                    ToolCallRow.task_id == str(task_id), ToolCallRow.call_id == call_id
                )
            )
            return _to_record(row) if row else None

    async def reserve(self, call: ToolCallRecord) -> bool:
        safe = call.redacted()
        if safe.task_id is None or not safe.call_id:
            raise ValueError("A durable tool reservation needs task_id and call_id")
        async with self._session() as session:
            statement = upsert(session, ToolCallRow).values(
                task_id=str(safe.task_id),
                call_id=safe.call_id,
                tool=safe.tool,
                input=safe.input_data,
                output={},
                success=False,
                completed=False,
                error=None,
                latency_ms=0,
                interface=safe.interface.value,
                created_at=safe.created_at,
            )
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=["task_id", "call_id"])
            )
            return bool(result.rowcount)

    async def complete(self, call: ToolCallRecord) -> None:
        safe = call.redacted()
        if safe.task_id is None or not safe.call_id:
            raise ValueError("A durable tool completion needs task_id and call_id")
        async with self._session() as session:
            result = await session.execute(
                update(ToolCallRow)
                .where(
                    ToolCallRow.task_id == str(safe.task_id),
                    ToolCallRow.call_id == safe.call_id,
                )
                .values(
                    output=safe.output,
                    success=safe.success,
                    completed=True,
                    error=safe.error,
                    latency_ms=safe.latency_ms,
                    interface=safe.interface.value,
                )
            )
            if not result.rowcount:
                raise RuntimeError("Tool call was completed without a reservation")


class InMemoryToolCallLog:
    """Implements `domain.tools.telemetry.ToolCallLog`."""

    def __init__(self) -> None:
        self.calls: list[ToolCallRecord] = []

    async def record(self, call: ToolCallRecord) -> None:
        self.calls.append(call.redacted())

    async def list_for_task(self, task_id: UUID) -> list[ToolCallRecord]:
        return [call for call in self.calls if call.task_id == task_id]

    async def get_call(self, task_id: UUID, call_id: str) -> ToolCallRecord | None:
        return next(
            (call for call in self.calls if call.task_id == task_id and call.call_id == call_id),
            None,
        )

    async def reserve(self, call: ToolCallRecord) -> bool:
        if call.task_id is None or not call.call_id:
            raise ValueError("A durable tool reservation needs task_id and call_id")
        if await self.get_call(call.task_id, call.call_id) is not None:
            return False
        self.calls.append(call.redacted())
        return True

    async def complete(self, call: ToolCallRecord) -> None:
        if call.task_id is None or not call.call_id:
            raise ValueError("A durable tool completion needs task_id and call_id")
        for index, existing in enumerate(self.calls):
            if existing.task_id == call.task_id and existing.call_id == call.call_id:
                self.calls[index] = call.redacted()
                return
        raise RuntimeError("Tool call was completed without a reservation")
