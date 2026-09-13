"""Adding a document, re-indexing one, and taking one away.

The lifecycle, and it decides nothing about how work is done. It reads a file,
cuts the text into passages, asks whatever the router chose to embed them, and
writes the result. What an employee may do with a document, whether a task needs
approval, who does the work: all of that was decided before this module existed.

Four rules, each rejecting the version that looks simpler.

**Extraction, chunking and embedding are separate states, and the document is
saved between them.** A machine with no embedding model still gets a searchable
document - lexical retrieval answers from the text, and configuring a model
later is a re-index rather than a re-upload. A document that failed to extract
says so on its own record instead of vanishing.

**The same text added twice is one document.** The checksum is of the extracted
text, so re-adding a file after fixing a typo replaces what was indexed rather
than leaving both versions answering the same question.

**Nothing here is guarded the way memory is.** Remembering is a side effect of
work and must never fail a run; adding a document is the work the user asked
for, and a failure they are not told about is a document they think is there.
So this raises, and the interface reports it.

**Deleting a document deletes its passages and nothing else.** Memory written
while working with it stays: what an employee learned is a fact about what
happened, not a copy of the document (ADR 0016).
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import UUID

import structlog

from domain.errors import NotFoundError, PrometheusError
from domain.knowledge.chunking import chunk as split
from domain.knowledge.models import Chunk, Document, DocumentStatus
from domain.knowledge.protocols import EmbeddingProvider, KnowledgeStore, TextExtractor
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How many passages are embedded per call. A batch, because indexing a document
#: is hundreds of passages and a call each over a local server is minutes.
BATCH = 32


class DocumentNotFoundError(NotFoundError):
    """Named a document this workspace does not have."""


class KnowledgeService:
    def __init__(
        self,
        *,
        store: KnowledgeStore,
        extractors: TextExtractor,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self._store = store
        self._extractors = extractors
        self._embeddings = embeddings

    # --- Reading --------------------------------------------------------------

    async def list(
        self, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Document]:
        return await self._store.list(workspace_id=workspace_id)

    async def get(self, document_id: UUID) -> Document | None:
        return await self._store.get(document_id)

    async def passages(self, document_id: UUID) -> list[Chunk]:
        return await self._store.chunks_for(document_id)

    # --- Adding ---------------------------------------------------------------

    async def add_file(
        self,
        path: Path,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        title: str = "",
        media_type: str = "",
    ) -> Document:
        """Read a file on this machine and make it searchable in this workspace.

        By path rather than by bytes: the platform is local-first, the file the
        user dropped on the window is already here, and copying it through the
        request would make the interface a second file store (the same reasoning
        as `Attachment` at the interface boundary).
        """
        resolved = path.expanduser()
        if not resolved.is_file():
            raise PrometheusError(f"There is no file at {resolved}")
        text = self._extractors.extract(resolved, media_type)
        return await self.add_text(
            text,
            title=title or resolved.name,
            source=str(resolved),
            media_type=media_type,
            workspace_id=workspace_id,
            size_bytes=resolved.stat().st_size,
        )

    async def add_text(
        self,
        text: str,
        *,
        title: str,
        source: str = "",
        media_type: str = "text/plain",
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        size_bytes: int = 0,
    ) -> Document:
        """Store text as a document. What `add_file` becomes once the file is read."""
        if not text.strip():
            raise PrometheusError(f"{title} has no text in it.")
        checksum = sha256(text.encode("utf-8")).hexdigest()
        existing = await self._store.by_checksum(checksum, workspace_id=workspace_id)
        document = (
            existing
            if existing is not None
            else Document.create(title, workspace_id=workspace_id)
        )
        document = document.to(
            DocumentStatus.EXTRACTED,
            title=title.strip() or document.title,
            source=source or document.source,
            media_type=media_type or document.media_type,
            checksum=checksum,
            size_bytes=size_bytes or len(text.encode("utf-8")),
            error="",
        )
        await self._store.save(document)
        return await self._index(document, text)

    async def replace_file(self, document_id: UUID, path: Path) -> Document:
        """Put a newer version of the file in place of what this document holds.

        The same document afterwards - its id, its title - with the new file's
        text and passages. Adding the new version instead would leave two
        documents that disagree, both quoted with a source, and nothing to say
        which one is current.
        """
        document = await self._store.get(document_id)
        if document is None:
            raise DocumentNotFoundError(f"Unknown document: {document_id}")
        resolved = path.expanduser()
        if not resolved.is_file():
            raise PrometheusError(f"There is no file at {resolved}")
        text = self._extractors.extract(resolved, "")
        if not text.strip():
            raise PrometheusError(f"{resolved.name} has no text in it.")
        updated = document.to(
            DocumentStatus.EXTRACTED,
            source=str(resolved),
            checksum=sha256(text.encode("utf-8")).hexdigest(),
            size_bytes=resolved.stat().st_size,
            error="",
        )
        await self._store.save(updated)
        return await self._index(updated, text)

    async def reindex(self, document_id: UUID) -> Document:
        """Cut and embed a document again, with whatever model is configured now.

        The case this exists for is a changed embedding model: every chunk says
        what embedded it, and vectors from another model are not comparable with
        a query's, so they are skipped by retrieval until this has run
        (ADR 0016). The text comes from the passages already stored, so a
        document whose source file has since moved is still re-indexable.
        """
        document = await self._store.get(document_id)
        if document is None:
            raise DocumentNotFoundError(f"Unknown document: {document_id}")
        stored = await self._store.chunks_for(document_id)
        text = "\n\n".join(chunk.content for chunk in stored)
        if not text.strip():
            raise PrometheusError(f"{document.title} has no stored text to re-index.")
        return await self._index(document, text)

    async def reindex_stale(
        self, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> int:
        """Re-index every document embedded by a model other than today's.

        Called after the embedding model is changed. Without it the new model
        was chosen, every stored vector became incomparable with a query's, and
        retrieval quietly fell back to matching words - for a person who had
        just picked a better model and would see answers get worse. Returns how
        many were re-indexed; none when nothing can embed, since re-indexing
        would only strip the vectors that are there.
        """
        model = self._embeddings.model if self._embeddings is not None else ""
        if not model:
            return 0
        done = 0
        for document in await self._store.list(workspace_id=workspace_id):
            chunks = await self._store.chunks_for(document.id)
            if chunks and any(chunk.embedding_model != model for chunk in chunks):
                await self._index(document, "\n\n".join(chunk.content for chunk in chunks))
                done += 1
        if done:
            log.info("knowledge.reindexed_for_model", model=model, documents=done)
        return done

    async def delete(self, document_id: UUID) -> bool:
        return await self._store.delete(document_id)

    # --- The part that costs -------------------------------------------------

    async def _index(self, document: Document, text: str) -> Document:
        passages = split(text)
        chunks = [
            Chunk.create(document, ordinal, passage)
            for ordinal, passage in enumerate(passages)
        ]
        embedded, status = await self._embed(chunks)
        await self._store.replace_chunks(document.id, embedded)
        finished = document.to(status, chunk_count=len(embedded))
        await self._store.save(finished)
        log.info(
            "knowledge.indexed",
            document_id=str(document.id),
            chunks=len(embedded),
            status=status.value,
            model=self._embeddings.model if self._embeddings else "",
        )
        return finished

    async def _embed(self, chunks: list[Chunk]) -> tuple[list[Chunk], DocumentStatus]:
        """Vectors for every passage, or the passages alone and a status saying so.

        A failing embedding server is not a failed upload. The text is stored
        and searchable lexically, the document says EXTRACTED rather than
        INDEXED, and `reindex` finishes the job once the server is back - which
        is the difference between a document somebody has to add again and one
        the platform can complete on its own.
        """
        if self._embeddings is None or not self._embeddings.model or not chunks:
            return chunks, DocumentStatus.EXTRACTED if chunks else DocumentStatus.FAILED
        model = self._embeddings.model
        done: list[Chunk] = []
        for start in range(0, len(chunks), BATCH):
            batch = chunks[start : start + BATCH]
            try:
                vectors = await self._embeddings.embed([one.content for one in batch])
            except Exception as error:
                log.warning("knowledge.embedding_failed", error=str(error))
                return chunks, DocumentStatus.EXTRACTED
            done.extend(
                one.embedded_by(model, vector)
                for one, vector in zip(batch, vectors, strict=True)
            )
        return done, DocumentStatus.INDEXED
