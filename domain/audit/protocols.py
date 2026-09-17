"""What was done here, by whom, and what came of it.

Writing and reading are two protocols rather than one, for the same reason
`Memory` and `MemoryMaintenance` are separate: the executor needs to append and
must never be able to read the whole history back, and holding the appender is
not an argument for holding the reader.

`result` is a string rather than an enum because an audit line is a record of
what happened, and the three values it takes today - SUCCESS, FAILURE, DENIED -
are the schema's business. Widening the vocabulary must not mean a domain change
that every existing row has to be migrated for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from domain.policies.models import ActorKind
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


@dataclass(frozen=True, slots=True)
class AuditRecord:
    action: str
    actor_kind: ActorKind
    result: str
    actor_id: str | None = None
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    task_id: UUID | None = None
    assignment_id: UUID | None = None
    tool: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditLog(Protocol):
    async def record(self, record: AuditRecord) -> None: ...


class AuditTrail(Protocol):
    """The read side. Newest first, because that is the question people ask."""

    async def recent(
        self,
        *,
        limit: int = 50,
        task_id: UUID | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> list[AuditRecord]: ...

    async def verify(self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID):
        """Verify the hash chain and its durable head checkpoint."""
        ...
