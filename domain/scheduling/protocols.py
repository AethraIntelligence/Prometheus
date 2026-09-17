"""How the scheduler reaches what it needs, and nothing more."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from domain.scheduling.models import Event, Schedule
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class ScheduleRepository(Protocol):
    async def save(self, schedule: Schedule) -> None: ...

    async def get(self, schedule_id: UUID) -> Schedule | None: ...

    async def list(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Schedule]: ...

    async def due(
        self, now: datetime, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Schedule]:
        """The time-based schedules that should have fired by `now`.

        `now` is passed in rather than read from a clock inside the adapter, so
        the same question can be asked of a moment that has not arrived - which
        is how this is tested without waiting for it.
        """
        ...

    async def delete(self, schedule_id: UUID) -> bool: ...

    async def claim(
        self, schedule_id: UUID, owner: str, now: datetime, lease_seconds: int
    ) -> bool:
        """Atomically lease one firing. False while another process owns it."""
        ...

    async def renew(
        self, schedule_id: UUID, owner: str, now: datetime, lease_seconds: int
    ) -> bool: ...

    async def release(self, schedule_id: UUID, owner: str) -> bool: ...


class EventLog(Protocol):
    async def record(self, event: Event) -> None: ...

    async def pending(
        self, kind: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, limit: int = 20
    ) -> list[Event]:
        """Events of this kind that nothing has acted on yet, oldest first."""
        ...

    async def recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, limit: int = 50
    ) -> list[Event]: ...

    async def consume(self, event_id: UUID, now: datetime | None = None) -> bool:
        """Claim an event. False when somebody else already had it."""
        ...
