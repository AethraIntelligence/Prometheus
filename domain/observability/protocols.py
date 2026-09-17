"""Write, read and maintenance capabilities for observability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from domain.observability.models import TraceEvent, TraceView
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


@dataclass(frozen=True, slots=True)
class TraceHealth:
    available: bool
    queued: int = 0
    dropped: int = 0
    last_error: str = ""
    exporter: str = "disabled"


class TraceSink(Protocol):
    async def emit(self, event: TraceEvent) -> None: ...


class TraceRepository(TraceSink, Protocol):
    async def get(self, identifier: UUID) -> TraceView | None: ...

    async def recent(
        self,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        *,
        limit: int = 50,
        entity_type: str = "",
        entity_id: str = "",
    ) -> list[TraceView]: ...

    async def prune(self, workspace_id: WorkspaceId, before: datetime) -> int: ...

    def health(self) -> TraceHealth: ...
