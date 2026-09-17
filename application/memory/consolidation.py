"""Keeping memory from growing into noise (§9.7).

Three mechanisms, and they do different jobs:

* **Time to live** removes what was only ever true for a while - the working
  notes of a task that ended hours ago. That is `prune`, and it needs no model.
* **Decay** pushes the old below the recent without deleting it, so a fact from
  March is still there when nothing newer answers the question. That is
  `domain.memory.ranking`, and it needs no model either.
* **Summarisation** is the only one that does. Thirty episodes saying "sorted
  the invoices folder, it worked" are one thing worth knowing, and no amount of
  ranking turns them into it. So when the episodic record passes a threshold,
  the oldest batch is folded into a single semantic item and the originals are
  forgotten.

The threshold is what keeps this affordable: consolidation is not run per task,
it is run when there is enough to consolidate. And it is guarded like every
other memory operation - a summariser that fails leaves the episodes in place,
which is a store that is larger than it should be rather than one that has lost
something.
"""

from __future__ import annotations

import structlog

from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.protocols import LLM
from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    Provenance,
    SourceKind,
)
from domain.memory.protocols import Memory, MemoryMaintenance
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How many episodes have to accumulate before folding any of them is worth a
#: model call. Below this, recall's own ranking is a better filter than a
#: summary would be.
THRESHOLD = 12

#: How many are folded at once. Fewer than the threshold on purpose: the recent
#: episodes are the ones still being recalled, and summarising them would
#: replace detail that is still in use.
BATCH = 8


class Consolidator:
    """Folds an accumulation of episodes into one thing worth remembering."""

    def __init__(
        self,
        llm: LLM,
        memory: Memory,
        maintenance: MemoryMaintenance,
        *,
        threshold: int = THRESHOLD,
        batch: int = BATCH,
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._maintenance = maintenance
        self._threshold = threshold
        self._batch = batch

    async def consolidate(
        self, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> MemoryItem | None:
        """Fold the oldest episodes if there are enough of them. Returns what was written."""
        try:
            await self._maintenance.prune(workspace_id=workspace_id)
            episodes = await self._memory.recall(
                MemoryQuery(
                    workspace_id=workspace_id,
                    scopes=frozenset({MemoryScope.WORKSPACE}),
                    kinds=frozenset({MemoryKind.EPISODIC}),
                    limit=self._threshold * 2,
                )
            )
        except Exception as error:
            log.warning("memory.consolidation_read_failed", error=str(error))
            return None

        if len(episodes) < self._threshold:
            return None

        # Oldest first, and only the oldest batch: what is still being recalled
        # is what is recent, and summarising that would take away detail that is
        # in use to save space that is not short.
        oldest = sorted(episodes, key=lambda item: item.created_at)[: self._batch]
        summary = await self._summarise(oldest)
        if not summary:
            return None

        folded = MemoryItem.create(
            summary,
            scope=MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
            workspace_id=workspace_id,
            # Worth more than any one episode it replaces: it is the only record
            # of them now, and it says something none of them said alone.
            importance=min(max(item.importance for item in oldest) + 0.1, 1.0),
            metadata={"consolidated": len(oldest)},
            # A model's reading of several accounts: an assumption, however
            # good, and never more trusted than the least trusted thing it read.
            basis=MemoryBasis.INFERRED,
            confidence=round(min(item.confidence for item in oldest) * 0.9, 3),
            provenance=Provenance(
                kind=SourceKind.CONSOLIDATION,
                label=f"{len(oldest)} earlier outcomes",
                # The originals are forgotten below; their ids and sources are
                # what is left of where this came from.
                derived_from=tuple(
                    dict.fromkeys(
                        (item.provenance.ref or str(item.id)) for item in oldest
                    )
                ),
            ),
        )
        try:
            await self._memory.remember(folded)
            # Written before the originals go: an interruption between the two
            # leaves a duplicated memory, which recall tolerates. The other
            # order loses the episodes and has nothing that replaced them.
            await self._maintenance.forget([item.id for item in oldest])
        except Exception as error:
            log.warning("memory.consolidation_write_failed", error=str(error))
            return None

        log.info("memory.consolidated", folded=len(oldest), workspace_id=str(workspace_id))
        return folded

    async def _summarise(self, items: list[MemoryItem]) -> str:
        episodes = "\n".join(f"- {item.content}" for item in items)
        try:
            response = await self._llm.generate(
                LLMRequest(
                    messages=(Message.user(render("memory_consolidation", episodes=episodes)),),
                    temperature=0.0,
                )
            )
        except Exception as error:
            log.warning("memory.summarisation_failed", error=str(error))
            return ""
        return " ".join(response.content.split())[:800]

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """Folding a list of one-line outcomes is the cheapest work here.

        EXTRACTION rather than SYNTHESIS: the answer the user reads is
        synthesis and is worth a good model, and this is housekeeping that
        happens on its own after somebody else's task finished.
        """
        return (
            TaskKind.EXTRACTION,
            CapabilityRequirement(),
            RoutingHints(quality=0.3, cost_sensitivity=0.9),
        )
