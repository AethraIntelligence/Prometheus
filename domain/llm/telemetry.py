"""Cost and latency accounting.

Local development pays for every token, and a looping agent gets expensive
before it gets wrong. So this is recorded from the first call, not added once
there is a bill to explain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from domain.llm.models import Usage
from domain.secrets.models import redact


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    provider: str
    model: str
    usage: Usage
    success: bool
    task_id: UUID | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Why this model ran: the kind of work, the catalog entry, the router's
    #: reason and the escalation level. Empty where nothing routed the call.
    task_kind: str = ""
    entry: str = ""
    reason: str = ""
    escalation_level: int = 0

    def redacted(self) -> LLMCallRecord:
        return LLMCallRecord(
            provider=self.provider,
            model=self.model,
            usage=self.usage,
            success=self.success,
            task_id=self.task_id,
            error=redact(self.error),
            created_at=self.created_at,
            task_kind=self.task_kind,
            entry=self.entry,
            reason=redact(self.reason),
            escalation_level=self.escalation_level,
        )


@dataclass(frozen=True, slots=True)
class SpendSummary:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class LLMCallLog(Protocol):
    """Where every model call is accounted for."""

    async def record(self, call: LLMCallRecord) -> None: ...

    async def total(self, task_id: UUID | None = None) -> SpendSummary: ...

    async def list_for_task(self, task_id: UUID) -> list[LLMCallRecord]: ...
