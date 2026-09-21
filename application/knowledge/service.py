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

**Indexing says where it has got to.** Reading a file, cutting it and embedding
the pieces is minutes on a local model, and it happens inside the request that
asked for it - so without this, an interface has a button that does nothing
visible for two minutes and then a list that changed. The report is state the
monitor holds (`progress`), not an event somebody had to be watching for.
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import structlog

from application.knowledge.progress import IndexingMonitor
from domain.errors import NotFoundError, PrometheusError
from domain.knowledge.chunking import chunk as split
from domain.knowledge.chunking import rejoin
from domain.knowledge.models import (
    Chunk,
    Document,
    DocumentStatus,
    IndexingProgress,
    IndexingStage,
)
from domain.knowledge.protocols import (
    EmbeddingProvider,
    IndexingObserver,
    KnowledgeStore,
    TextExtractor,
)
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
        observer: IndexingObserver | None = None,
    ) -> None:
        self._store = store
        self._extractors = extractors
        self._embeddings = embeddings
        #: Always built, with no flag and no wiring: a surface that asks what is
        #: happening gets an answer on every installation, and one that does not
        #: pays a dict nobody reads. An `observer` is a second listener beside
        #: it, never instead of it.
        self._monitor = IndexingMonitor()
        self._observer = observer

    # A `Sequence`, because this class has a method called `list` and the
    # builtin is not reachable as a type inside it.
    def progress(self) -> Sequence[IndexingProgress]:
        """What is being read or embedded at this moment, plus what just ended."""
        return self._monitor.active()

    def _report(
        self,
        key: str,
        title: str,
        stage: IndexingStage,
        *,
        done: int = 0,
        total: int = 0,
        document_id: UUID | None = None,
        error: str = "",
    ) -> None:
        progress = IndexingProgress(
            key=key,
            title=title,
            stage=stage,
            done=done,
            total=total,
            document_id=document_id,
            error=error,
        )
        self._monitor.report(progress)
        if self._observer is not None:
            self._observer.report(progress)

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
        # The path is the key until there is a document: the person chose files
        # and is watching rows for them, and a row that appears only once the
        # text has been read is a row that appears after the slowest stage.
        key = str(resolved)
        name = title or resolved.name
        self._report(key, name, IndexingStage.READING)
        try:
            text = self._extractors.extract(resolved, media_type)
        except Exception as error:
            self._report(key, name, IndexingStage.FAILED, error=str(error))
            raise
        return await self.add_text(
            text,
            title=name,
            source=str(resolved),
            media_type=media_type,
            workspace_id=workspace_id,
            size_bytes=resolved.stat().st_size,
            key=key,
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
        key: str = "",
    ) -> Document:
        """Store text as a document. What `add_file` becomes once the file is read."""
        watching = key or title
        if not text.strip():
            self._report(
                watching, title, IndexingStage.FAILED, error=f"{title} has no text in it."
            )
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
        return await self._index(document, text, key=watching)

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
        # An existing document is watched by its id, so the row already on the
        # screen is the one that moves rather than a second one beside it.
        key = str(document.id)
        self._report(key, document.title, IndexingStage.READING, document_id=document.id)
        try:
            text = self._extractors.extract(resolved, "")
        except Exception as error:
            self._report(
                key,
                document.title,
                IndexingStage.FAILED,
                document_id=document.id,
                error=str(error),
            )
            raise
        if not text.strip():
            said = f"{resolved.name} has no text in it."
            self._report(
                key, document.title, IndexingStage.FAILED, document_id=document.id, error=said
            )
            raise PrometheusError(said)
        updated = document.to(
            DocumentStatus.EXTRACTED,
            source=str(resolved),
            checksum=sha256(text.encode("utf-8")).hexdigest(),
            size_bytes=resolved.stat().st_size,
            error="",
        )
        await self._store.save(updated)
        return await self._index(updated, text, key=key)

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
        # `rejoin`, not a join: passages overlap on purpose, and joining them
        # plainly writes every overlap into the text again. Four re-indexes of
        # one document made it twice its own length, sentences repeated at each
        # boundary - and since changing the embedding model re-indexes
        # everything, nobody had to ask for it even once.
        text = rejoin([chunk.content for chunk in stored])
        key = str(document.id)
        if not text.strip():
            said = f"{document.title} has no stored text to re-index."
            self._report(
                key, document.title, IndexingStage.FAILED, document_id=document.id, error=said
            )
            raise PrometheusError(said)
        return await self._index(document, text, key=key)

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
                await self._index(
                    document,
                    rejoin([chunk.content for chunk in chunks]),
                    key=str(document.id),
                )
                done += 1
        if done:
            log.info("knowledge.reindexed_for_model", model=model, documents=done)
        return done

    async def delete(self, document_id: UUID) -> bool:
        return await self._store.delete(document_id)

    # --- The part that costs -------------------------------------------------

    async def _index(self, document: Document, text: str, *, key: str = "") -> Document:
        watching = key or str(document.id)
        self._report(
            watching, document.title, IndexingStage.CHUNKING, document_id=document.id
        )
        passages = split(text)
        chunks = [
            Chunk.create(document, ordinal, passage)
            for ordinal, passage in enumerate(passages)
        ]
        embedded, status = await self._embed(
            chunks, watching=watching, title=document.title, document_id=document.id
        )
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
        # FAILED is the document's own state and not a failure of the request:
        # a machine with no embedding model stores text and says so. What ended
        # is reported as what the document became, so the screen and the record
        # cannot disagree about it.
        self._report(
            watching,
            document.title,
            IndexingStage.FAILED if status is DocumentStatus.FAILED else IndexingStage.DONE,
            done=len(embedded),
            total=len(embedded),
            document_id=document.id,
            error=finished.error,
        )
        return finished

    async def _embed(
        self,
        chunks: Sequence[Chunk],
        *,
        watching: str = "",
        title: str = "",
        document_id: UUID | None = None,
    ) -> tuple[Sequence[Chunk], DocumentStatus]:
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
            if watching:
                self._report(
                    watching,
                    title,
                    IndexingStage.EMBEDDING,
                    done=start,
                    total=len(chunks),
                    document_id=document_id,
                )
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
