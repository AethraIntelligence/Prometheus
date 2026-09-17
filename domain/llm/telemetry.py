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


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    provider: str
    model: str
    usage: Usage
    success: bool
    task_id: UUID | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
