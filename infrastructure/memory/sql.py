"""Memory in a SQL database. SQL does not leave this package.

One adapter for both backends. What differs between them is the text search -
FTS5 on SQLite, `tsvector` on PostgreSQL - and that difference is written twice
here rather than abstracted into something that is neither. Two adapters would
have been two things to keep true, and the second one would fall behind
(ADR 0017).

The text search is the only part of the system that knows which index it is.
It is used the way an index should be used - to narrow, not to decide: the index
answers *which items mention this*, and `domain.memory.ranking` answers *which
of those are worth reading now*. Ranking by BM25 alone would return the best
textual match to "the report", which is every report ever written.

The rank from the index is turned into a relevance between 0 and 1 rather than
passed through raw, because the domain multiplies it by importance and decay,
and a score whose scale depends on the backend would make the policy depend on
the backend too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from re import findall
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.memory.access import visible
from domain.memory.models import (
    WORKSPACE_BOUND,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
)
from domain.memory.ranking import CUTOFF_RATIO, best_of, score
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence import memory_fts
from infrastructure.persistence.dialect import is_postgres, upsert
from infrastructure.persistence.models import MemoryItemRow
from infrastructure.persistence.session import session_scope

#: How many index hits to score before cutting to the caller's limit. The index
#: ranks by text alone; the ranking that matters also weighs importance and age,
#: so it needs more than `limit` rows to have anything to reorder.
CANDIDATE_FACTOR = 5


#: A goal is prose, and prose contains quotes, colons and hyphens - operators in
#: FTS5's query language and in `tsquery` alike, any one of which turns a search
#: into a syntax error. So both backends are handed extracted words rather than
#: the sentence, and neither query language is ever given text it could be
#: injected through.


def _to_row(item: MemoryItem) -> dict[str, object]:
    return {
        "id": str(item.id),
        "workspace_id": str(item.workspace_id),
        "scope": item.scope.value,
        "kind": item.kind.value,
        "content": item.content,
        "employee_id": str(item.employee_id) if item.employee_id else None,
        "plan_id": str(item.plan_id) if item.plan_id else None,
        "task_id": str(item.task_id) if item.task_id else None,
        "metadata": dict(item.metadata),
        "importance": float(item.importance),
        "created_at": item.created_at,
        "expires_at": item.expires_at,
    }


def _to_item(row: MemoryItemRow) -> MemoryItem:
    return MemoryItem(
        id=UUID(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        scope=MemoryScope(row.scope),
        kind=MemoryKind(row.kind),
        content=row.content,
        employee_id=UUID(row.employee_id) if row.employee_id else None,
        plan_id=UUID(row.plan_id) if row.plan_id else None,
        task_id=UUID(row.task_id) if row.task_id else None,
        metadata=row.meta or {},
        importance=row.importance,
        created_at=_aware(row.created_at),
        expires_at=_aware(row.expires_at) if row.expires_at else None,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SqlMemory:
    """Implements `domain.memory.protocols.Memory` and `MemoryMaintenance`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        try:
            async with session_scope(self._session_factory) as session:
                yield session
        except OperationalError as error:
            message = str(error.orig)
            if "no such table" in message or "unable to open database file" in message:
                raise StorageNotInitializedError(
                    "The local database has no schema yet."
                ) from error
            raise StorageError(message) from error

    async def remember(self, item: MemoryItem) -> None:
        values = _to_row(item)
        async with self._session() as session:
            # Against the table rather than the mapped class: the JSON column is
            # called `metadata`, which a declarative class cannot use as an
            # attribute name, and the insert is by column name either way.
            statement = upsert(session, MemoryItemRow.__table__).values(**values)
            # Upsert on the id: consolidation rewrites an item's content, and a
            # second row saying the same thing under a new id would make the
            # thing it replaced un-findable rather than replaced.
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[MemoryItemRow.__table__.c.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "created_at")},
                )
            )

    async def recall(self, query: MemoryQuery) -> list[MemoryItem]:
        if not query.scopes or query.limit <= 0:
            return []
        now = query.as_of or datetime.now(UTC)
        async with self._session() as session:
            ranked = await self._matching(session, query)
            if ranked is not None and not ranked:
                return []  # the search ran and nothing mentioned it

            # The workspace narrows the scopes it owns and not the two above
            # it: a stated preference and what is known about this machine were
            # written in some workspace and are not about it. The rule itself is
            # `domain/memory/access.py`; this is the index agreeing with it.
            bound = [
                scope.value for scope in query.scopes if scope in WORKSPACE_BOUND
            ]
            unbound = [
                scope.value for scope in query.scopes if scope not in WORKSPACE_BOUND
            ]
            statement = select(MemoryItemRow).where(
                or_(
                    and_(
                        MemoryItemRow.workspace_id == str(query.workspace_id),
                        MemoryItemRow.scope.in_(bound),
                    ),
                    MemoryItemRow.scope.in_(unbound),
                )
            )
            if query.kinds:
                statement = statement.where(
                    MemoryItemRow.kind.in_([kind.value for kind in query.kinds])
                )
            if ranked is not None:
                statement = statement.where(MemoryItemRow.id.in_(list(ranked)))
            rows = await session.scalars(
                statement.order_by(MemoryItemRow.created_at.desc()).limit(
                    max(query.limit * CANDIDATE_FACTOR, query.limit)
                )
            )
            candidates = [_to_item(row) for row in rows]

        # The SQL narrowed; the access rule decides. Applying it here as well
        # means an index that has drifted returns fewer rows, never another
        # employee's (see `domain/memory/access.py`).
        scored = [
            (score(item, relevance=(ranked or {}).get(str(item.id), 1.0), now=now), item)
            for item in candidates
            if visible(item, query)
        ]
        # A search is ranked and cut; a listing is only ranked. See `best_of`.
        cutoff = CUTOFF_RATIO if ranked is not None else 0.0
        return best_of(scored, query.limit, cutoff=cutoff)

    async def forget(
        self, ids: Sequence[UUID], *, within: MemoryQuery | None = None
    ) -> int:
        if not ids:
            return 0
        wanted = [str(i) for i in ids]
        async with self._session() as session:
            if within is not None:
                # Read before deleting, and decided by the domain's rule rather
                # than restated as a WHERE clause: the same double check
                # `recall` makes, for the same reason - SQL that drifted from
                # `visible()` must delete less, never somebody else's row.
                rows = await session.scalars(
                    select(MemoryItemRow).where(MemoryItemRow.id.in_(wanted))
                )
                wanted = [
                    str(item.id) for item in map(_to_item, rows) if visible(item, within)
                ]
                if not wanted:
                    return 0
            result = await session.execute(
                delete(MemoryItemRow).where(MemoryItemRow.id.in_(wanted))
            )
            return int(result.rowcount or 0)

    async def prune(
        self,
        *,
        now: datetime | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> int:
        moment = now or datetime.now(UTC)
        async with self._session() as session:
            result = await session.execute(
                delete(MemoryItemRow).where(
                    MemoryItemRow.workspace_id == str(workspace_id),
                    MemoryItemRow.expires_at.is_not(None),
                    MemoryItemRow.expires_at <= moment,
                )
            )
            return int(result.rowcount or 0)

    # --- The index ------------------------------------------------------------

    async def _matching(
        self, session: AsyncSession, query: MemoryQuery
    ) -> dict[str, float] | None:
        """Item ids the text index matched, and how strongly.

        `None` means no text was asked for, which is a listing rather than a
        search and must not be confused with a search that found nothing.
        """
        words = findall(r"\w+", query.text)
        words = [word for word in words if len(word) > 1]
        if not words:
            return None
        limit = max(query.limit * CANDIDATE_FACTOR, 20)
        if is_postgres(session):
            return await self._postgres_matching(session, words, limit)
        rows = await session.execute(
            text(
                "SELECT item_id, rank FROM memory_items_fts "
                "WHERE memory_items_fts MATCH :expression "
                "ORDER BY rank LIMIT :limit"
            ),
            {"expression": " OR ".join(f'"{word}"' for word in words), "limit": limit},
        )
        hits = rows.all()
        if not hits:
            return {}
        # FTS5's rank is negative and unbounded below, better meaning smaller.
        # Expressed as a fraction of the best hit, so a result half as good as
        # the best scores half as much whatever the absolute numbers are - and
        # the domain's cutoff means the same thing on every backend.
        best = min(float(rank) for _, rank in hits)
        return {
            str(item_id): min(max(float(rank) / best, 0.0), 1.0) if best else 1.0
            for item_id, rank in hits
        }

    async def _postgres_matching(
        self, session: AsyncSession, words: list[str], limit: int
    ) -> dict[str, float]:
        """The same question asked of `tsvector`, and the same answer shape.

        `ts_rank` is positive and larger-is-better, the opposite of FTS5's rank,
        so it is normalised the other way round - and what leaves this method is
        a fraction of the best hit either way, which is what makes the domain's
        cutoff mean the same thing on both backends (ADR 0016).

        The words are joined with `|` and asked of `to_tsquery`, because the
        other branch asks FTS5 for any of them. `plainto_tsquery` was here and
        joins with `and`: on PostgreSQL "where is the report" then matched
        nothing, since no note contains every word of the question a person
        typed. Same question, two answers - which is the drift ADR 0016 exists
        to prevent, and Phase 19 found it by running the memory tests on the
        second dialect. The words reach here already reduced to `\\w+`, so there
        is no operator among them for `to_tsquery` to trip over.
        """
        vector = memory_fts.POSTGRES_VECTOR.format(column="content")
        rows = await session.execute(
            text(
                f"SELECT id, ts_rank({vector}, query) AS rank "
                "FROM memory_items, to_tsquery('simple', :words) AS query "
                f"WHERE {vector} @@ query "
                "ORDER BY rank DESC LIMIT :limit"
            ),
            {"words": " | ".join(words), "limit": limit},
        )
        hits = rows.all()
        if not hits:
            return {}
        best = max(float(rank) for _, rank in hits)
        return {
            str(item_id): min(max(float(rank) / best, 0.0), 1.0) if best else 1.0
            for item_id, rank in hits
        }
