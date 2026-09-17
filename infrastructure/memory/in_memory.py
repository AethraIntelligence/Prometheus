"""Memory that lives for the length of the process.

What the tests run against, and what an in-memory container gets. It is a real
implementation rather than a stub: the ranking, the scoping and the expiry are
the domain's, so the only thing that changes between this and SQLite is where
the rows are and how the text search is done.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from domain.memory.access import visible
from domain.memory.models import MemoryItem, MemoryQuery
from domain.memory.ranking import CUTOFF_RATIO, best_of, is_live, retention_over, score
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

_WORD = re.compile(r"\w+", re.UNICODE)


def relevance(text: str, content: str) -> float:
    """How well a stored item answers a query, without an index.

    Term overlap, which is a poorer ranking than BM25 and the same *shape* of
    one: a number between 0 and 1 that the domain's scoring multiplies. An item
    matching nothing scores zero and drops out, which is what makes an empty
    query and a non-matching query different.
    """
    wanted = {word.lower() for word in _WORD.findall(text)}
    if not wanted:
        return 1.0
    found = {word.lower() for word in _WORD.findall(content)}
    return len(wanted & found) / len(wanted)


class InMemoryMemory:
    """Implements `domain.memory.protocols.Memory` and `MemoryMaintenance`."""

    def __init__(self) -> None:
        self._items: dict[UUID, MemoryItem] = {}

    async def remember(self, item: MemoryItem) -> None:
        self._items[item.id] = deepcopy(item)

    async def recall(self, query: MemoryQuery) -> list[MemoryItem]:
        scored: list[tuple[float, MemoryItem]] = []
        for item in self._items.values():
            if not visible(item, query):
                continue
            weight = relevance(query.text, item.content)
            if weight <= 0:
                continue
            scored.append((score(item, relevance=weight, now=query.as_of), item))
        cutoff = CUTOFF_RATIO if query.text.strip() else 0.0
        return [deepcopy(item) for item in best_of(scored, query.limit, cutoff=cutoff)]

    async def forget(
        self, ids: Sequence[UUID], *, within: MemoryQuery | None = None
    ) -> int:
        chosen = [
            item_id
            for item_id in ids
            if item_id in self._items
            and (within is None or visible(self._items[item_id], within))
        ]
        return sum(1 for item_id in chosen if self._items.pop(item_id, None) is not None)

    async def prune(
        self,
        *,
        now: datetime | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> int:
        expired = [
            item.id
            for item in self._items.values()
            if item.workspace_id == workspace_id
            and (not is_live(item, now) or retention_over(item, now))
        ]
        return await self.forget(expired)
