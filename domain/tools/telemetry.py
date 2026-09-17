"""What a tool call cost and whether it worked.

Model calls have been accounted for since Phase 2 for a reason that applies to
tools just as much: a loop that is failing does not announce itself, it just
keeps calling. Recorded per call, arguments redacted, so a run can be read back
afterwards without leaking a credential into the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from domain.computer.interfaces import InterfaceLevel
from domain.secrets.models import redact


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    tool: str
    success: bool
    latency_ms: int = 0
    task_id: UUID | None = None
    input_data: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    #: Provider call id, unique inside one task. It is the durable idempotency
    #: key for a tool invocation; providers may reuse a short id in another
    #: task, so it is never unique by itself.
    call_id: str | None = None
    #: False means the action was reserved before execution but the process did
    #: not durably record its result. Such a call is deliberately not repeated:
    #: an external side effect may already have happened.
    completed: bool = True
    #: Which level of the interface hierarchy this call reached the world at.
    #: Stored rather than derived, so a trace read months later still says how
    #: the work was done even if the tool has since been re-declared.
    interface: InterfaceLevel = InterfaceLevel.API
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def redacted(self) -> ToolCallRecord:
        """The form that is safe to store: no argument named like a credential."""
        return ToolCallRecord(
            tool=self.tool,
            success=self.success,
            latency_ms=self.latency_ms,
            task_id=self.task_id,
            input_data=redact(self.input_data),
            output=redact(self.output),
            error=redact(self.error),
            call_id=self.call_id,
            completed=self.completed,
            interface=self.interface,
            created_at=self.created_at,
        )


class ToolCallLog(Protocol):
    """Where every tool call is accounted for."""

    async def record(self, call: ToolCallRecord) -> None: ...

    async def list_for_task(self, task_id: UUID) -> list[ToolCallRecord]: ...

    async def get_call(self, task_id: UUID, call_id: str) -> ToolCallRecord | None: ...

    async def reserve(self, call: ToolCallRecord) -> bool:
        """Persist intent before execution; false when the key already exists."""
        ...

    async def complete(self, call: ToolCallRecord) -> None: ...
