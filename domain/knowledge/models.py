"""Knowledge: what the user brought, as values.

A document is not a memory (ADR 0016). Memory decays, expires and is recalled
hedged - "you noted last time"; a document was put here on purpose, does not
get less true in March than it was in January, and is *quoted* with a source
rather than remembered. So it has its own values, its own store and its own
contract, and the two meet in the context assembler and nowhere else.

Two fields exist because of the failure they prevent.

`embedding_model` and `embedding_dimension` are on every chunk. Without them,
changing the embedding model - or moving to another store - leaves nobody able
to say whether the vectors already written are comparable with the ones a query
produces, and the answer becomes a guess. With them, a mismatch means
re-indexing, which is work; without them, it means silently comparing numbers
that mean different things, which is a wrong answer nobody can see.

`checksum` is what makes adding the same file twice an update rather than a
second copy of it. A person who fixed a typo and re-added the file wants the
corrected text searched, not both versions of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

#: A vector, in the only form the domain knows about: numbers, in order. What
#: produced them is on the chunk; how they are compared is the retriever's.
Vector = tuple[float, ...]


class DocumentStatus(StrEnum):
    """Where a document is between arriving and being searchable.

    `EXTRACTED` is a real state and not a step in a function: text that could be
    read out of a file is worth keeping even when no embedding model is
    configured on this machine, because lexical retrieval still answers from it
    and configuring one later is a re-index rather than a re-upload.
    """

    PENDING = "PENDING"
    EXTRACTED = "EXTRACTED"
    INDEXED = "INDEXED"
    FAILED = "FAILED"


class IndexingStage(StrEnum):
    """Where a document is inside the one operation a person is waiting on.

    The stages are the ones that cost time and can be reported honestly:
    reading the file, cutting it into passages, embedding them. `EMBEDDING` is
    the only one with a fraction under it, because it is the only one done in
    countable pieces - a batch at a time (`KnowledgeService.BATCH`). Reporting a
    made-up percentage for the other two would be an interface lying about work
    it cannot see.
    """

    READING = "READING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class IndexingProgress:
    """What is happening to one document right now.

    Keyed by `key` rather than by the document id, because the first stage runs
    before there is a document: a file is read, and only text that came out of
    it becomes a record. So a person adding three files sees three rows from the
    moment they chose them, and each acquires its id as it gets one.

    This is not a `ProgressEvent`. That vocabulary is six kinds belonging to a
    task an employee runs (`domain/tasks/progress.py`), and adding a seventh for
    a document nobody was assigned would move the coupling into every interface
    rather than remove it. Indexing is not work the workforce did - it is the
    lifecycle of something the user handed over, and it is reported as state
    somebody can read at any moment rather than as events they must have been
    watching for.
    """

    #: The file's path while that is all there is, the document id once there
    #: is one. Stable for the whole operation either way.
    key: str
    title: str
    stage: IndexingStage
    #: Passages embedded and passages in total. Both zero until the text has
    #: been cut, and total stays zero where nothing can embed.
    done: int = 0
    total: int = 0
    document_id: UUID | None = None
    error: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def fraction(self) -> float:
        """How much of the countable part is finished, or 0.0 where nothing is.

        Zero means "no fraction to show", not "none of it done": an interface
        draws a bar that is moving rather than one that is empty, which is the
        difference between "working" and "stuck".
        """
        return self.done / self.total if self.total else 0.0

    @property
    def finished(self) -> bool:
        return self.stage in (IndexingStage.DONE, IndexingStage.FAILED)


@dataclass(frozen=True, slots=True)
class Document:
    """One thing the user brought, and where it came from."""

    id: UUID
    workspace_id: WorkspaceId
    title: str
    source: str = ""
    media_type: str = ""
    status: DocumentStatus = DocumentStatus.PENDING
    #: Of the extracted text, not of the file: two exports of one page that
    #: read the same are the same document as far as anything here cares.
    checksum: str = ""
    size_bytes: int = 0
    chunk_count: int = 0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        title: str,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        **extra: Any,
    ) -> Document:
        return cls(id=uuid4(), workspace_id=workspace_id, title=title.strip(), **extra)

    def to(self, status: DocumentStatus, **changes: Any) -> Document:
        from dataclasses import replace

        return replace(self, status=status, updated_at=datetime.now(UTC), **changes)

    @property
    def is_searchable(self) -> bool:
        return self.status in (DocumentStatus.EXTRACTED, DocumentStatus.INDEXED)

    @property
    def needs_indexing(self) -> bool:
        """Text without meaning: the embedding server was down, or never there.

        A document embedded by a model since replaced is re-indexed when the
        model changes, so this is the one case a person has to start.
        """
        return self.status is not DocumentStatus.INDEXED


@dataclass(frozen=True, slots=True)
class Chunk:
    """A passage of a document, with what embedded it written down beside it."""

    id: UUID
    document_id: UUID
    workspace_id: WorkspaceId
    ordinal: int
    content: str
    embedding: Vector | None = None
    embedding_model: str = ""
    embedding_dimension: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        document: Document,
        ordinal: int,
        content: str,
        **extra: Any,
    ) -> Chunk:
        return cls(
            id=uuid4(),
            document_id=document.id,
            workspace_id=document.workspace_id,
            ordinal=ordinal,
            content=content,
            **extra,
        )

    def embedded_by(self, model: str, vector: Vector) -> Chunk:
        from dataclasses import replace

        return replace(
            self,
            embedding=tuple(vector),
            embedding_model=model,
            embedding_dimension=len(vector),
        )

    def comparable_with(self, model: str, dimension: int) -> bool:
        """Whether this chunk's vector means the same thing as a query's.

        Both halves are checked. A model of the same name that changed its
        output size is a different model, and a name that matches while the
        length does not is the case where comparing anyway would produce a
        number rather than an error.
        """
        return (
            self.embedding is not None
            and self.embedding_model == model
            and self.embedding_dimension == dimension
        )


@dataclass(frozen=True, slots=True)
class Passage:
    """A chunk that answered a query, and why it was chosen.

    Carries the document's title and source because the point of a chunk is that
    it can be cited: a quotation whose origin has been dropped between the store
    and the prompt is a memory, and this is deliberately not one.
    """

    chunk: Chunk
    title: str
    source: str = ""
    score: float = 0.0
    #: How each half of the search rated it, kept for the trace: a retrieval
    #: that only ever answers lexically is a machine with no embedding model
    #: configured, and that should be visible rather than inferred.
    lexical: float = 0.0
    semantic: float = 0.0

    @property
    def citation(self) -> str:
        return f"{self.title} ({self.source})" if self.source else self.title


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    """Every retrieval is scoped, exactly as every recall is."""

    text: str
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    limit: int = 5
    #: Narrow to particular documents. Empty means everything the workspace has.
    document_ids: frozenset[UUID] = field(default_factory=frozenset)
