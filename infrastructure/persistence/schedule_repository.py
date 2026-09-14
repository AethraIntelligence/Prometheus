"""Schedules and events, stored where they survive the process.

The one thing worth stating about both adapters: **`now` is passed in, never
read here.** `due(now)` is a question about a moment, and a store that consulted
a clock of its own could not be asked about a moment that has not arrived - so
the whole of Phase 12's scheduling would be tested by waiting for it.

Times are stored naive and read back as UTC, which is the convention the rest of
this schema already follows (SQLite has no timezone type, and a column that
sometimes carries an offset is worse than one that never does).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, time
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.scheduling.models import Event, Recurrence, Schedule
from domain.workforce.directions import ApprovalChoice
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import EventRow, ScheduleRow
from infrastructure.persistence.session import session_scope


def _naive(moment: datetime | None) -> datetime | None:
    return moment.astimezone(UTC).replace(tzinfo=None) if moment else None


def _aware(moment: datetime | None) -> datetime | None:
    return moment.replace(tzinfo=UTC) if moment else None


def _to_schedule(row: ScheduleRow) -> Schedule:
    recurrence = None
    if row.every_seconds is not None:
        recurrence = Recurrence(every_seconds=row.every_seconds)
    elif row.daily_at:
        recurrence = Recurrence(daily_at=time.fromisoformat(row.daily_at))
    return Schedule(
        id=UUID(row.id),
        request=row.request,
        workspace_id=WorkspaceId(row.workspace_id),
        name=row.name,
        recurrence=recurrence,
        on_event=row.on_event,
        enabled=row.enabled,
        next_due_at=_aware(row.next_due_at),
        last_run_at=_aware(row.last_run_at),
        last_objective_id=UUID(row.last_objective_id) if row.last_objective_id else None,
        runs=row.runs,
        created_at=_aware(row.created_at) or datetime.now(UTC),
        conversation_id=UUID(row.conversation_id) if row.conversation_id else None,
        model=row.model or "",
        approvals=_approvals(row.approvals),
    )


def _approvals(value: str | None) -> ApprovalChoice:
    try:
        return ApprovalChoice(value or "ASK")
    except ValueError:
        return ApprovalChoice.ASK


def _to_event(row: EventRow) -> Event:
    return Event(
        id=UUID(row.id),
        kind=row.kind,
        workspace_id=WorkspaceId(row.workspace_id),
        payload=dict(row.payload or {}),
        source=row.source,
        created_at=_aware(row.created_at) or datetime.now(UTC),
        consumed_at=_aware(row.consumed_at),
    )


class _SqliteBase:
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


class SqlScheduleRepository(_SqliteBase):
    """Implements `domain.scheduling.protocols.ScheduleRepository`."""

    async def save(self, schedule: Schedule) -> None:
        values = {
            "id": str(schedule.id),
            "workspace_id": str(schedule.workspace_id),
            "name": schedule.name,
            "request": schedule.request,
            "every_seconds": (
                schedule.recurrence.every_seconds if schedule.recurrence else None
            ),
            "daily_at": (
                schedule.recurrence.daily_at.isoformat(timespec="minutes")
                if schedule.recurrence and schedule.recurrence.daily_at
                else None
            ),
            "on_event": schedule.on_event,
            "enabled": schedule.enabled,
            "next_due_at": _naive(schedule.next_due_at),
            "last_run_at": _naive(schedule.last_run_at),
            "last_objective_id": (
                str(schedule.last_objective_id) if schedule.last_objective_id else None
            ),
            "runs": schedule.runs,
            "created_at": _naive(schedule.created_at),
            "conversation_id": (
                str(schedule.conversation_id) if schedule.conversation_id else None
            ),
            "model": schedule.model,
            "approvals": schedule.approvals.value,
        }
        async with self._session() as session:
            statement = upsert(session, ScheduleRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ScheduleRow.id],
                    set_={k: v for k, v in values.items() if k != "id"},
                )
            )

    async def get(self, schedule_id: UUID) -> Schedule | None:
        async with self._session() as session:
            row = await session.get(ScheduleRow, str(schedule_id))
            return _to_schedule(row) if row else None

    async def list(self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID) -> list[Schedule]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ScheduleRow)
                .where(ScheduleRow.workspace_id == str(workspace_id))
                .order_by(ScheduleRow.created_at)
            )
            return [_to_schedule(row) for row in rows]

    async def due(
        self, now: datetime, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Schedule]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ScheduleRow)
                .where(
                    ScheduleRow.workspace_id == str(workspace_id),
                    ScheduleRow.enabled.is_(True),
                    ScheduleRow.next_due_at.is_not(None),
                    ScheduleRow.next_due_at <= _naive(now),
                )
                .order_by(ScheduleRow.next_due_at)
            )
            return [_to_schedule(row) for row in rows]

    async def delete(self, schedule_id: UUID) -> bool:
        async with self._session() as session:
            result = await session.execute(
                delete(ScheduleRow).where(ScheduleRow.id == str(schedule_id))
            )
            return bool(result.rowcount)


class SqlEventLog(_SqliteBase):
    """Implements `domain.scheduling.protocols.EventLog`."""

    async def record(self, event: Event) -> None:
        async with self._session() as session:
            session.add(
                EventRow(
                    id=str(event.id),
                    workspace_id=str(event.workspace_id),
                    kind=event.kind,
                    payload=dict(event.payload),
                    source=event.source,
                    created_at=_naive(event.created_at),
                    consumed_at=_naive(event.consumed_at),
                )
            )

    async def pending(
        self, kind: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, limit: int = 20
    ) -> list[Event]:
        async with self._session() as session:
            rows = await session.scalars(
                select(EventRow)
                .where(
                    EventRow.workspace_id == str(workspace_id),
                    EventRow.kind == kind,
                    EventRow.consumed_at.is_(None),
                )
                .order_by(EventRow.created_at)
                .limit(limit)
            )
            return [_to_event(row) for row in rows]

    async def recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, limit: int = 50
    ) -> list[Event]:
        async with self._session() as session:
            rows = await session.scalars(
                select(EventRow)
                .where(EventRow.workspace_id == str(workspace_id))
                .order_by(EventRow.created_at.desc())
                .limit(limit)
            )
            return [_to_event(row) for row in rows]

    async def consume(self, event_id: UUID, now: datetime | None = None) -> bool:
        """Claim it, and say whether the claim was ours.

        The check and the write are one statement on purpose: two passes that
        both read "not consumed" and then both wrote would each fire, which is
        the exact duplication `consumed_at` exists to prevent.
        """
        stamp = _naive(now or datetime.now(UTC))
        async with self._session() as session:
            result = await session.execute(
                EventRow.__table__.update()
                .where(EventRow.id == str(event_id), EventRow.consumed_at.is_(None))
                .values(consumed_at=stamp)
            )
            return bool(result.rowcount)


class InMemoryScheduleRepository:
    """The same contract, for tests and for a machine with no database."""

    def __init__(self) -> None:
        self._schedules: dict[UUID, Schedule] = {}

    async def save(self, schedule: Schedule) -> None:
        self._schedules[schedule.id] = deepcopy(schedule)

    async def get(self, schedule_id: UUID) -> Schedule | None:
        found = self._schedules.get(schedule_id)
        return deepcopy(found) if found else None

    async def list(self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID) -> list[Schedule]:
        return [
            deepcopy(schedule)
            for schedule in sorted(self._schedules.values(), key=lambda s: s.created_at)
            if schedule.workspace_id == workspace_id
        ]

    async def due(
        self, now: datetime, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Schedule]:
        return [
            schedule for schedule in await self.list(workspace_id) if schedule.is_due(now)
        ]

    async def delete(self, schedule_id: UUID) -> bool:
        return self._schedules.pop(schedule_id, None) is not None


class InMemoryEventLog:
    def __init__(self) -> None:
        self._events: dict[UUID, Event] = {}

    async def record(self, event: Event) -> None:
        self._events[event.id] = deepcopy(event)

    async def pending(
        self, kind: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, limit: int = 20
    ) -> list[Event]:
        found = [
            deepcopy(event)
            for event in sorted(self._events.values(), key=lambda e: e.created_at)
            if event.kind == kind
            and event.workspace_id == workspace_id
            and not event.consumed
        ]
        return found[:limit]

    async def recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, limit: int = 50
    ) -> list[Event]:
        found = [
            deepcopy(event)
            for event in sorted(self._events.values(), key=lambda e: e.created_at, reverse=True)
            if event.workspace_id == workspace_id
        ]
        return found[:limit]

    async def consume(self, event_id: UUID, now: datetime | None = None) -> bool:
        event = self._events.get(event_id)
        if event is None or event.consumed:
            return False
        self._events[event_id] = event.consume(now)
        return True
