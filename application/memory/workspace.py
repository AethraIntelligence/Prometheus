"""The manager's half of memory: what this workspace knows, in general.

Prometheus reads and writes at a different grain from an employee. An employee
remembers how a task went; the manager remembers how the user wants things done
here, and hands what it knows down to the tasks it delegates - which is why what
it recalls goes into `SharedContext.facts` rather than into a prompt of its own.
Passing it down means the employee sees it in the one place it already looks,
and the same narrowing rules apply to it as to everything else in an assignment.

This is deliberately a small object over the same `Memory` contract, not a
second store. There is one memory; there are two grains of question asked of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import structlog

from application.memory.assembler import record_uses
from application.memory.recorder import MemoryRecorder, trim
from domain.memory.citation import recollection
from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    Provenance,
    SourceKind,
)
from domain.memory.protocols import Memory
from domain.memory.ranking import expires_at
from domain.memory.usage import MemoryUseLog
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How much the manager passes down. Smaller than an employee's own budget: this
#: is prepended to every task in a plan, so its cost is per task, not per run.
DEFAULT_LIMIT = 4


class WorkspaceMemory:
    """What is known about working here, for the manager to read and add to."""

    def __init__(
        self,
        memory: Memory,
        recorder: MemoryRecorder,
        *,
        limit: int = DEFAULT_LIMIT,
        uses: MemoryUseLog | None = None,
    ) -> None:
        self._memory = memory
        self._recorder = recorder
        self._limit = limit
        self._uses = uses

    async def context_for(
        self,
        request: str,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        objective_id: UUID | None = None,
    ) -> tuple[str, ...]:
        """What is worth knowing before working on this request, with what it rests on."""
        query = MemoryQuery(
            text=request,
            workspace_id=workspace_id,
            scopes=frozenset({MemoryScope.WORKSPACE, MemoryScope.USER}),
            # Preferences and what became of past work. Not WORKING:
            # another run's half-finished notes are noise here.
            kinds=frozenset({MemoryKind.SEMANTIC, MemoryKind.EPISODIC}),
            limit=self._limit,
        )
        try:
            items = await self._memory.recall(query)
        except Exception as error:
            log.warning("memory.recall_failed", error=str(error))
            return ()
        items = [item for item in items if item.content.strip()]
        await record_uses(
            self._uses, items, query, reader="manager", objective_id=objective_id
        )
        return tuple(recollection(item) for item in items)

    async def remember_preferences(
        self,
        preferences: Sequence[str],
        *,
        source: str = "",
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        objective_id: UUID | None = None,
    ) -> None:
        """How the user wants work done from now on, as read out of one request.

        Standing preferences only. What this request asked for - the folder, the
        file name, the count - is a constraint on this request and is not kept
        (§9.4, and the first validation run's finding).
        """
        await self._recorder.record_preferences(
            preferences,
            workspace_id=workspace_id,
            source=source,
            objective_id=str(objective_id) if objective_id else "",
        )

    async def remember_answer(
        self,
        request: str,
        answer: str,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        objective_id: UUID | None = None,
    ) -> None:
        """A request answered without delegating anything (§7.5).

        Worth keeping precisely because no task ran: nothing else in the system
        would have a record that this was asked and what was said.
        """
        await self._recorder.remember(
            MemoryItem.create(
                f"Asked: {trim(request, 200)}\nAnswered directly: {trim(answer, 400)}",
                scope=MemoryScope.WORKSPACE,
                kind=MemoryKind.EPISODIC,
                workspace_id=workspace_id,
                importance=0.5,
                expires_at=expires_at(MemoryKind.EPISODIC),
                # Nothing ran: this is what a model said, recorded as such.
                basis=MemoryBasis.INFERRED,
                confidence=0.4,
                provenance=Provenance(
                    kind=SourceKind.OBJECTIVE,
                    ref=str(objective_id) if objective_id else "",
                    label=trim(request, 200),
                ),
            )
        )
