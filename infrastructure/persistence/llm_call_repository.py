"""Where model spend is recorded. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.llm.models import Usage
from domain.llm.telemetry import LLMCallRecord, SpendSummary
from infrastructure.persistence.models import LLMCallRow
from infrastructure.persistence.session import session_scope


class SqlLLMCallLog:
    """Implements `domain.llm.telemetry.LLMCallLog`."""

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

    async def record(self, call: LLMCallRecord) -> None:
        async with self._session() as session:
            session.add(
                LLMCallRow(
                    task_id=str(call.task_id) if call.task_id else None,
                    provider=call.provider,
                    model=call.model,
                    prompt_tokens=call.usage.prompt_tokens,
                    output_tokens=call.usage.output_tokens,
                    cost_usd=call.usage.cost_usd,
                    latency_ms=call.usage.latency_ms,
                    success=call.success,
                    error=call.error,
                    created_at=call.created_at,
                )
            )

    async def total(self, task_id: UUID | None = None) -> SpendSummary:
        statement = select(
            func.count(LLMCallRow.id),
            func.coalesce(func.sum(LLMCallRow.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCallRow.output_tokens), 0),
            func.coalesce(func.sum(LLMCallRow.cost_usd), 0.0),
        )
        if task_id is not None:
            statement = statement.where(LLMCallRow.task_id == str(task_id))

        async with self._session() as session:
            calls, prompt_tokens, output_tokens, cost = (await session.execute(statement)).one()

        return SpendSummary(
            calls=int(calls),
            prompt_tokens=int(prompt_tokens),
            output_tokens=int(output_tokens),
            cost_usd=float(cost),
        )

    async def list_for_task(self, task_id: UUID) -> list[LLMCallRecord]:
        async with self._session() as session:
            rows = await session.scalars(
                select(LLMCallRow)
                .where(LLMCallRow.task_id == str(task_id))
                .order_by(LLMCallRow.created_at, LLMCallRow.id)
            )
            return [
                LLMCallRecord(
                    provider=row.provider,
                    model=row.model,
                    usage=Usage(
                        prompt_tokens=row.prompt_tokens,
                        output_tokens=row.output_tokens,
                        cost_usd=row.cost_usd,
                        latency_ms=row.latency_ms,
                    ),
                    success=row.success,
                    task_id=task_id,
                    error=row.error,
                    created_at=row.created_at,
                )
                for row in rows
            ]


class InMemoryLLMCallLog:
    """Implements `domain.llm.telemetry.LLMCallLog` for tests and dry runs."""

    def __init__(self) -> None:
        self.calls: list[LLMCallRecord] = []

    async def record(self, call: LLMCallRecord) -> None:
        self.calls.append(call)

    async def total(self, task_id: UUID | None = None) -> SpendSummary:
        selected = [c for c in self.calls if task_id is None or c.task_id == task_id]
        return SpendSummary(
            calls=len(selected),
            prompt_tokens=sum(c.usage.prompt_tokens for c in selected),
            output_tokens=sum(c.usage.output_tokens for c in selected),
            cost_usd=sum(c.usage.cost_usd for c in selected),
        )

    async def list_for_task(self, task_id: UUID) -> list[LLMCallRecord]:
        return [call for call in self.calls if call.task_id == task_id]
