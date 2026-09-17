"""Memory values: what is kept, whose it is, and what may be asked for."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class MemoryScope(StrEnum):
    """Scope is an access boundary, not a hint.

    An employee never reads another employee's EMPLOYEE_PRIVATE memory.

    Three of these are inside a workspace and two are above it. USER is the
    person rather than the context they are working in: "always answer in
    Markdown" was not said about the sales folder, and re-stating it in every
    workspace is a chore the platform would be creating for itself. SYSTEM is
    the installation - what has been learned about this machine, which no
    workspace owns either.

    The cost is stated rather than hidden: a preference stated at work applies
    at home too. The workspace it was stated in is kept on the item as
    provenance, so a future "only here" is a narrowing of an existing record and
    not a migration.
    """

    WORKSPACE = "WORKSPACE"
    PLAN = "PLAN"
    EMPLOYEE_PRIVATE = "EMPLOYEE_PRIVATE"
    USER = "USER"
    SYSTEM = "SYSTEM"


#: The scopes a workspace boundary applies to. Everything else is above it, and
#: `domain/memory/access.py` is the one place that difference is expressed.
WORKSPACE_BOUND: frozenset[MemoryScope] = frozenset(
    {MemoryScope.WORKSPACE, MemoryScope.PLAN, MemoryScope.EMPLOYEE_PRIVATE}
)


class MemoryKind(StrEnum):
    WORKING = "WORKING"
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"


class MemoryBasis(StrEnum):
    """What a memory rests on - the difference between a fact and an assumption.

    Before this, every recollection reached a prompt as the same kind of line,
    so "the user said: always Markdown" and a model's summary of eight episodes
    were weighed alike. They are not alike: the first is evidence, the second is
    somebody's reading of evidence, and a run that cannot tell them apart will
    act on a guess as firmly as on an instruction.

    * STATED - a person said it, directly or in a request.
    * OBSERVED - the platform recorded it happening (which tools reached work).
    * REPORTED - an employee said it happened; checked by a verifier at most.
    * INFERRED - a model's generalisation: a summary, a direct answer.
    """

    STATED = "STATED"
    OBSERVED = "OBSERVED"
    REPORTED = "REPORTED"
    INFERRED = "INFERRED"


#: Which bases count as fact when shown to a model. The other two are rendered
#: as claims to be checked, never merged into the facts.
FACTUAL_BASES: frozenset[MemoryBasis] = frozenset({MemoryBasis.STATED, MemoryBasis.OBSERVED})


class MemoryStatus(StrEnum):
    """Whether a memory is still the platform's current belief.

    SUPERSEDED is kept rather than deleted: a correction that erased what it
    corrected would leave no trace of why a run last week did what it did, and
    the row is pruned after a retention period instead (`ranking`). CONTESTED
    means two memories disagree and nothing settled which is right - both are
    still recalled, and both say so.
    """

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    CONTESTED = "CONTESTED"


class SourceKind(StrEnum):
    """Where a memory came from, so it can be traced back to a record."""

    PERSON = "PERSON"
    OBJECTIVE = "OBJECTIVE"
    TASK = "TASK"
    CONSOLIDATION = "CONSOLIDATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Provenance:
    """The record a memory can be followed back to.

    `ref` is an id in the store - an objective, a task - and `label` is what a
    person would recognise it by. `derived_from` names the memories a summary
    replaced: they are forgotten once folded, and without their ids the summary
    would be a statement with no visible origin.
    """

    kind: SourceKind = SourceKind.UNKNOWN
    ref: str = ""
    label: str = ""
    derived_from: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: UUID
    workspace_id: WorkspaceId
    scope: MemoryScope
    kind: MemoryKind
    content: str
    employee_id: UUID | None = None
    plan_id: UUID | None = None
    task_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    basis: MemoryBasis = MemoryBasis.INFERRED
    #: How far this is to be trusted, 0 to 1. Set by whoever writes it, from
    #: the basis and the outcome - never by a model rating itself.
    confidence: float = 0.5
    provenance: Provenance = field(default_factory=Provenance)
    status: MemoryStatus = MemoryStatus.ACTIVE
    superseded_by: UUID | None = None
    #: When the status last changed. What the retention of a superseded memory
    #: is counted from.
    revised_at: datetime | None = None
    #: The memories this one disagrees with, where nothing decided between them.
    contradicts: tuple[UUID, ...] = ()

    @property
    def is_factual(self) -> bool:
        return self.basis in FACTUAL_BASES

    @property
    def is_active(self) -> bool:
        return self.status is not MemoryStatus.SUPERSEDED

    @classmethod
    def create(
        cls,
        content: str,
        *,
        scope: MemoryScope,
        kind: MemoryKind,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        **extra: Any,
    ) -> MemoryItem:
        return cls(
            id=uuid4(),
            workspace_id=workspace_id,
            scope=scope,
            kind=kind,
            content=content,
            **extra,
        )


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """Every recall is scoped. There is no unscoped read."""

    text: str = ""
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    scopes: frozenset[MemoryScope] = field(
        default_factory=lambda: frozenset({MemoryScope.WORKSPACE})
    )
    kinds: frozenset[MemoryKind] = field(default_factory=frozenset)
    employee_id: UUID | None = None
    plan_id: UUID | None = None
    task_id: UUID | None = None
    limit: int = 20
    #: The moment the query is asked as of. Decay and expiry are read from it,
    #: so a test can age memory without waiting for it and a long run reads its
    #: own memory consistently rather than against a clock that moved mid-run.
    as_of: datetime | None = None
    #: Only these items, where given. Still subject to every other bound: an id
    #: from another workspace is not found rather than returned.
    ids: frozenset[UUID] = field(default_factory=frozenset)
    #: Whether superseded memories are returned. A run never reads them; a
    #: person tracing why something changed does.
    include_superseded: bool = False
