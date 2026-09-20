"""A record of which memories a piece of work was given, and why.

Recall is invisible by nature: a line of context arrives and a run goes
differently. Without this record "why did it put the report in last month's
folder" has no answer but a guess, and the phase's promise - that a person can
see why a memory was used - is a promise about exactly that question.

Not stored as part of memory. A use is an event about work, like an audit line:
it names the memory by id and keeps no copy of its content, so forgetting a
memory really forgets it, and the record says only that something since
forgotten was used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


@dataclass(frozen=True, slots=True)
class MemoryUse:
    memory_id: UUID
    reason: str
    weight: float = 0.0
    objective_id: UUID | None = None
    task_id: UUID | None = None
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    #: Which reader asked: the manager for the whole objective, or a task's run.
    reader: str = ""
    id: UUID = field(default_factory=uuid4)
    used_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryUseLog(Protocol):
    async def record(self, uses: list[MemoryUse]) -> None: ...

    async def for_objective(self, objective_id: UUID) -> list[MemoryUse]: ...

    async def for_task(self, task_id: UUID) -> list[MemoryUse]: ...

    async def for_memory(self, memory_id: UUID, *, limit: int = 20) -> list[MemoryUse]: ...

    async def used_by(self, objective_ids: Sequence[UUID]) -> set[UUID]:
        """Which of these objectives were given a memory at all.

        Asked for a whole thread at once, because the only question an
        interface has before somebody wants the detail is whether there is any:
        offering "Memory used" under an answer that was given none is an
        invitation to open an empty drawer. Reading each turn's uses in full to
        answer it would be one round trip per turn plus its plans, for a
        boolean.
        """
        ...
