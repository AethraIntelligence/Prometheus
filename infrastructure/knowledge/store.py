"""Documents and their passages. SQL does not leave this package.

One adapter for both backends, like memory's: what differs is the text index -
FTS5 on SQLite, `tsvector` on PostgreSQL - and the difference is written twice
rather than abstracted into something that is neither (ADR 0017).

Three things here are decisions rather than plumbing.

**A vector is bytes.** float32 in order, packed with `array`, because it is read
on every query and never read by a person. A JSON list of a thousand floats is
four times the file for no benefit - and the domain never sees the encoding,
which is what lets a store with a vector type of its own use that instead
(ADR 0017).

**Replacing a document's chunks is one operation.** A re-index that deleted the
old passages and then failed to write the new ones would leave a document that
exists, says it is indexed, and answers nothing.

**A search is two searches.** The text index narrows lexically and the vectors
are compared in memory, both normalised before the domain blends them
(`domain/knowledge/ranking.py`). At one person's scale - thousands of passages,
not millions - a cosine over the workspace's rows costs milliseconds and needs
no extension and no daemon, which is the whole argument of ADR 0017.
"""

from __future__ import annotations

from array import array
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from re import findall
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.knowledge.models import Chunk, Document, DocumentStatus, Vector
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence import knowledge_fts
from infrastructure.persistence.dialect import is_postgres, upsert
from infrastructure.persistence.models import ChunkRow, DocumentRow
from infrastructure.persistence.session import session_scope

#: The same treatment memory's index gets, and for the same reason: a question
#: is prose, prose contains quotes, colons and hyphens, and every one of them is
#: an operator in both query languages. Words are extracted and quoted, so
#: neither index is handed text it could be injected through.


#: The vector encoding, stated once. 'f' is float32: the precision an embedding
#: model's own output has, and half the bytes of float64 for a number whose
#: fourth decimal place decides nothing.
_TYPECODE = "f"


def pack(vector: Vector | None) -> bytes | None:
    return array(_TYPECODE, vector).tobytes() if vector else None


def unpack(raw: bytes | None) -> Vector | None:
    if not raw:
        return None
    numbers = array(_TYPECODE)
    numbers.frombytes(raw)
    return tuple(numbers)


def _to_document_row(document: Document) -> dict[str, Any]:
    return {
        "id": str(document.id),
        "workspace_id": str(document.workspace_id),
        "title": document.title,
        "source": document.source,
        "media_type": document.media_type,
        "status": document.status.value,
        "checksum": document.checksum,
        "size_bytes": int(document.size_bytes),
        "chunk_count": int(document.chunk_count),
        "error": document.error,
        "metadata": dict(document.metadata),
        "updated_at": datetime.now(UTC),
    }


def _to_document(row: DocumentRow) -> Document:
    return Document(
        id=UUID(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        title=row.title,
        source=row.source or "",
        media_type=row.media_type or "",
        status=DocumentStatus(row.status),
        checksum=row.checksum or "",
        size_bytes=int(row.size_bytes or 0),
        chunk_count=int(row.chunk_count or 0),
        error=row.error or "",
        metadata=row.meta or {},
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def _to_chunk(row: ChunkRow) -> Chunk:
    return Chunk(
        id=UUID(row.id),
        document_id=UUID(row.document_id),
        workspace_id=WorkspaceId(row.workspace_id),
        ordinal=int(row.ordinal),
        content=row.content,
        embedding=unpack(row.embedding),
        embedding_model=row.embedding_model or "",
        embedding_dimension=int(row.embedding_dimension or 0),
        metadata=row.meta or {},
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SqlKnowledgeStore:
    """Implements `domain.knowledge.protocols.KnowledgeStore`."""

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

    # --- Documents ------------------------------------------------------------

    async def save(self, document: Document) -> None:
        values = _to_document_row(document)
        async with self._session() as session:
            statement = upsert(session, DocumentRow.__table__).values(
                **values, created_at=document.created_at
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[DocumentRow.__table__.c.id],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in ("id", "created_at")
                    },
                )
            )

    async def get(self, document_id: UUID) -> Document | None:
        async with self._session() as session:
            row = await session.get(DocumentRow, str(document_id))
            return _to_document(row) if row else None

    async def list(
        self, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Document]:
        async with self._session() as session:
            found = await session.execute(
                select(DocumentRow)
                .where(DocumentRow.workspace_id == str(workspace_id))
                .order_by(DocumentRow.created_at.desc())
            )
            return [_to_document(row) for row in found.scalars()]

    async def by_checksum(
        self, checksum: str, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> Document | None:
        if not checksum:
            return None
        async with self._session() as session:
            found = await session.execute(
                select(DocumentRow).where(
                    DocumentRow.workspace_id == str(workspace_id),
                    DocumentRow.checksum == checksum,
                )
            )
            row = found.scalars().first()
            return _to_document(row) if row else None

    async def delete(self, document_id: UUID) -> bool:
        async with self._session() as session:
            # Explicit rather than relying on the cascade: SQLite enforces
            # foreign keys only when the pragma is on, and a chunk whose
            # document is gone is unreachable text that still answers queries.
            await session.execute(
                delete(ChunkRow).where(ChunkRow.document_id == str(document_id))
            )
            result = await session.execute(
                delete(DocumentRow).where(DocumentRow.id == str(document_id))
            )
            return bool(result.rowcount)

    # --- Chunks ---------------------------------------------------------------

    async def replace_chunks(self, document_id: UUID, chunks: Sequence[Chunk]) -> None:
        async with self._session() as session:
            await session.execute(
                delete(ChunkRow).where(ChunkRow.document_id == str(document_id))
            )
            if not chunks:
                return
            await session.execute(
                upsert(session, ChunkRow.__table__),
                [
                    {
                        "id": str(chunk.id),
                        "document_id": str(chunk.document_id),
                        "workspace_id": str(chunk.workspace_id),
                        "ordinal": int(chunk.ordinal),
                        "content": chunk.content,
                        "embedding": pack(chunk.embedding),
                        "embedding_model": chunk.embedding_model,
                        "embedding_dimension": int(chunk.embedding_dimension),
                        "metadata": dict(chunk.metadata),
                        "created_at": datetime.now(UTC),
                    }
                    for chunk in chunks
                ],
            )

    async def chunks_for(self, document_id: UUID) -> list[Chunk]:
        async with self._session() as session:
            found = await session.execute(
                select(ChunkRow)
                .where(ChunkRow.document_id == str(document_id))
                .order_by(ChunkRow.ordinal)
            )
            return [_to_chunk(row) for row in found.scalars()]

    # --- What the retriever needs ---------------------------------------------

    async def candidates(
        self,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        document_ids: frozenset[UUID] = frozenset(),
    ) -> list[tuple[Chunk, str, str]]:
        """Every passage in the workspace, with its document's title and source.

        Read whole rather than filtered by the index, because the semantic half
        of the search has to compare against all of them: an index cannot answer
        "which of these means the same thing". At one person's scale that is a
        few thousand short rows, and the cost of the honest version is
        milliseconds (ADR 0017).
        """
        async with self._session() as session:
            statement = (
                select(ChunkRow, DocumentRow.title, DocumentRow.source)
                .join(DocumentRow, DocumentRow.id == ChunkRow.document_id)
                .where(ChunkRow.workspace_id == str(workspace_id))
            )
            if document_ids:
                statement = statement.where(
                    ChunkRow.document_id.in_([str(one) for one in document_ids])
                )
            found = await session.execute(statement)
            return [
                (_to_chunk(row), title, source or "")
                for row, title, source in found.all()
            ]

    async def matching(
        self,
        query_text: str,
        limit: int,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        document_ids: frozenset[UUID] = frozenset(),
    ) -> dict[str, float]:
        """Chunk ids the text index matched, as a fraction of the best hit.

        Takes the user's words rather than a query expression: the syntax is
        the index's own business - and there are two indexes - so a caller
        building one would be a caller that knows which. Normalised here, as
        memory's rank is, so that what the domain blends is comparable between
        one index and another (ADR 0016).

        The scope is in the SQL and not in the caller, because the limit is
        applied here. Leaving the filtering to whoever reads the answer let
        forty passages of another workspace fill a limit of twenty, and the one
        passage in this one - which contained the word searched for - came back
        not at all. With no embedding model configured, that is a document that
        cannot be found by anything.
        """
        words = [word for word in findall(r"\w+", query_text) if len(word) > 1]
        if not words:
            return {}
        scope: dict[str, Any] = {"workspace_id": str(workspace_id)}
        narrowing = ""
        if document_ids:
            # Named one at a time rather than through `IN :ids`: the statement
            # is textual on both dialects, and an expanding bind parameter is
            # one more thing that differs between them.
            names = [f"doc_{index}" for index, _ in enumerate(document_ids)]
            scope.update(
                {name: str(one) for name, one in zip(names, document_ids, strict=True)}
            )
            narrowing = " AND chunks.document_id IN (" + ", ".join(
                f":{name}" for name in names
            ) + ")"
        async with self._session() as session:
            if is_postgres(session):
                # `|`, not `plainto_tsquery`'s implicit `and`: the other branch
                # asks FTS5 for any of the words, and a passage containing every
                # word of a typed question is rare enough that the two indexes
                # answered differently (Phase 19). The vector expression is the
                # index's own, so the index is actually used.
                vector = knowledge_fts.POSTGRES_VECTOR.format(column="content")
                rows = await session.execute(
                    text(
                        f"SELECT id, ts_rank({vector}, query) AS rank "
                        "FROM chunks, to_tsquery('simple', :words) AS query "
                        f"WHERE {vector} @@ query "
                        "AND chunks.workspace_id = :workspace_id"
                        f"{narrowing} "
                        "ORDER BY rank DESC LIMIT :limit"
                    ),
                    {"words": " | ".join(words), "limit": limit, **scope},
                )
                hits = rows.all()
                best = max((float(rank) for _, rank in hits), default=0.0)
            else:
                rows = await session.execute(
                    text(
                        "SELECT chunks_fts.chunk_id, chunks_fts.rank FROM chunks_fts "
                        "JOIN chunks ON chunks.id = chunks_fts.chunk_id "
                        "WHERE chunks_fts MATCH :expression "
                        "AND chunks.workspace_id = :workspace_id"
                        f"{narrowing} "
                        "ORDER BY chunks_fts.rank LIMIT :limit"
                    ),
                    {
                        "expression": " OR ".join(f'"{word}"' for word in words),
                        "limit": limit,
                        **scope,
                    },
                )
                hits = rows.all()
                # FTS5 ranks negative-and-smaller-is-better; `ts_rank` ranks
                # positive-and-larger-is-better. Both leave here as a fraction
                # of their own best hit, which is what the domain blends.
                best = min((float(rank) for _, rank in hits), default=0.0)
        if not hits or not best:
            return {str(chunk_id): 1.0 for chunk_id, _ in hits}
        return {
            str(chunk_id): min(max(float(rank) / best, 0.0), 1.0) for chunk_id, rank in hits
        }


class InMemoryKnowledgeStore:
    """The same contract without a file, for tests and an in-memory container."""

    def __init__(self) -> None:
        self._documents: dict[UUID, Document] = {}
        self._chunks: dict[UUID, list[Chunk]] = {}

    async def save(self, document: Document) -> None:
        self._documents[document.id] = document

    async def get(self, document_id: UUID) -> Document | None:
        return self._documents.get(document_id)

    async def list(
        self, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Document]:
        return sorted(
            (item for item in self._documents.values() if item.workspace_id == workspace_id),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def by_checksum(
        self, checksum: str, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> Document | None:
        if not checksum:
            return None
        for item in self._documents.values():
            if item.workspace_id == workspace_id and item.checksum == checksum:
                return item
        return None

    async def delete(self, document_id: UUID) -> bool:
        self._chunks.pop(document_id, None)
        return self._documents.pop(document_id, None) is not None

    async def replace_chunks(self, document_id: UUID, chunks: Sequence[Chunk]) -> None:
        self._chunks[document_id] = list(chunks)

    async def chunks_for(self, document_id: UUID) -> list[Chunk]:
        return list(self._chunks.get(document_id, ()))

    async def candidates(
        self,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        document_ids: frozenset[UUID] = frozenset(),
    ) -> list[tuple[Chunk, str, str]]:
        found = []
        for document_id, chunks in self._chunks.items():
            document = self._documents.get(document_id)
            if document is None or document.workspace_id != workspace_id:
                continue
            if document_ids and document_id not in document_ids:
                continue
            found.extend((chunk, document.title, document.source) for chunk in chunks)
        return found

    async def matching(
        self,
        query_text: str,
        limit: int,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        document_ids: frozenset[UUID] = frozenset(),
    ) -> dict[str, float]:
        """Word overlap, because there is no index here and there is no pretending.

        Deliberately crude: this exists so a test and an in-memory container can
        exercise the *blend* without a file, and a second search implementation
        that claimed to be the real one would be a second thing to keep true.
        It honours the scope, because a fake that answers a wider question than
        the real one hides exactly the defect this parameter was added for.
        """
        words = {word.casefold() for word in findall(r"\w+", query_text) if len(word) > 1}
        scores: dict[str, float] = {}
        for document_id, chunks in self._chunks.items():
            document = self._documents.get(document_id)
            if document is None or document.workspace_id != workspace_id:
                continue
            if document_ids and document_id not in document_ids:
                continue
            for chunk in chunks:
                content = chunk.content.casefold()
                hits = sum(1 for word in words if word in content)
                if hits:
                    scores[str(chunk.id)] = float(hits)
        best = max(scores.values(), default=0.0)
        ranked = sorted(scores.items(), key=lambda pair: -pair[1])[:limit]
        return {key: value / best for key, value in ranked} if best else {}
