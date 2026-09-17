"""Keeping memory consistent as it is written.

A standing memory - a preference, a note a person added - has no expiry, so
without this the only way a changed mind ever reached memory was the person
finding the old line and deleting it. Until then both were recalled, and a run
told "always answer in Markdown" and "always answer in plain text" picked one.

Two decisions, made by different things. Whether a new memory *replaces* or
*contradicts* an existing one is a judgement about meaning, asked of a cheap
model over a handful of candidates the store already found by their words. What
*follows* from that - supersede, or mark both contested - is the domain's rule
(`domain/memory/revision.py`) and is never left to the model.

Guarded like every other memory operation: a judge that cannot be reached or
cannot be read leaves the new memory ACTIVE and the old ones untouched. That is
a store with a duplicate in it, which a person can see and settle; the other
failure - superseding on an unreadable answer - silently loses what was said.
"""

from __future__ import annotations

import structlog

from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.llm.json_output import extract_object
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.protocols import LLM
from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    Provenance,
    SourceKind,
)
from domain.memory.protocols import Memory
from domain.memory.revision import Relation, revise

log = structlog.get_logger(__name__)

#: How many existing memories are shown to the judge. Found by their words, so
#: the real candidates are at the top and the tail is noise the judge would
#: have to be right about too.
CANDIDATES = 5


class MemoryReviser:
    """Writes a standing memory, superseding or contesting what it disagrees with."""

    def __init__(self, llm: LLM | None, memory: Memory, *, candidates: int = CANDIDATES) -> None:
        self._llm = llm
        self._memory = memory
        self._candidates = candidates

    async def remember(self, item: MemoryItem) -> MemoryItem:
        """Store `item` and revise what it bears on. Returns it as stored."""
        stored = item
        try:
            existing = await self._related(item)
            relations = await self._judge(item, existing) if existing else {}
            revised: list[MemoryItem] = []
            for index, old in enumerate(existing, start=1):
                relation = relations.get(index, Relation.UNRELATED)
                if relation is Relation.UNRELATED:
                    continue
                stored, old = revise(stored, old, relation)
                revised.append(old)
            # The new memory first: an interruption between the writes leaves
            # an extra memory, never a superseded one pointing at nothing.
            await self._memory.remember(stored)
            for old in revised:
                await self._memory.remember(old)
            if revised:
                log.info(
                    "memory.revised",
                    memory_id=str(stored.id),
                    status=stored.status.value,
                    affected=len(revised),
                )
            return stored
        except Exception as error:  # consistency is an improvement, not a precondition
            log.warning("memory.revision_failed", error=str(error))
            try:
                await self._memory.remember(item)
            except Exception as writing:
                log.warning("memory.write_failed", kind=item.kind.value, error=str(writing))
            return item

    async def _related(self, item: MemoryItem) -> list[MemoryItem]:
        if self._llm is None or self._candidates <= 0:
            return []
        # Scope is part of a memory's meaning, not only its visibility. A
        # workspace-specific exception may disagree with a global preference
        # and both still be true in their respective scopes; revising across
        # that boundary would silently change every other workspace.
        scopes = frozenset({item.scope})
        found = await self._memory.recall(
            MemoryQuery(
                text=item.content,
                workspace_id=item.workspace_id,
                scopes=scopes,
                kinds=frozenset({MemoryKind.SEMANTIC}),
                employee_id=item.employee_id,
                plan_id=item.plan_id,
                limit=self._candidates,
            )
        )
        return [other for other in found if other.id != item.id]

    async def _judge(
        self, item: MemoryItem, existing: list[MemoryItem]
    ) -> dict[int, Relation]:
        assert self._llm is not None
        response = await self._llm.generate(
            LLMRequest(
                messages=(
                    Message.user(
                        render(
                            "memory_revision",
                            new=item.content,
                            existing="\n".join(
                                f"{index}. {other.content}"
                                for index, other in enumerate(existing, start=1)
                            ),
                        )
                    ),
                ),
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        )
        parsed = extract_object(response.content)
        if parsed is None:
            log.warning("memory.revision_unreadable", memory_id=str(item.id))
            return {}
        relations: dict[int, Relation] = {}
        for field, relation in (
            ("contradicts", Relation.CONTRADICTS),
            # Second, so a number named in both reads as the update - the
            # milder reading, since superseding keeps the old memory on record.
            ("replaces", Relation.REPLACES),
        ):
            raw = parsed.get(field) or ()
            if not isinstance(raw, list | tuple):
                continue
            for value in raw:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= index <= len(existing):
                    relations[index] = relation
        return relations

    @staticmethod
    def correction(old: MemoryItem, content: str) -> tuple[MemoryItem, MemoryItem]:
        """A person rewriting a memory: the new text supersedes the old one.

        No model is asked. A person pointing at a line and changing it has
        already said how the two relate.
        """
        new = MemoryItem.create(
            content,
            scope=old.scope,
            kind=MemoryKind.SEMANTIC,
            workspace_id=old.workspace_id,
            employee_id=old.employee_id,
            plan_id=old.plan_id,
            importance=max(old.importance, 0.8),
            basis=MemoryBasis.STATED,
            confidence=1.0,
            provenance=Provenance(
                kind=SourceKind.PERSON,
                label="corrected by the user",
                derived_from=(str(old.id),),
            ),
            metadata={**old.metadata, "source": "person", "corrects": str(old.id)},
        )
        return revise(new, old, Relation.REPLACES)

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """Comparing a sentence with five others is cheap work, done rarely:
        only when a standing memory is written, never per task."""
        return (
            TaskKind.EXTRACTION,
            CapabilityRequirement(),
            RoutingHints(quality=0.5, cost_sensitivity=0.8),
        )
