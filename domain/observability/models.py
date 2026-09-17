"""The internal trace contract.

Execution repositories remain authoritative. These values are a bounded,
sanitized read model that connects their ids and records causal decisions that
do not otherwise have a row. Schema versions make an unknown future event
visible instead of inviting a reader to guess its meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4, uuid5

from domain.secrets.models import redact
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

TRACE_SCHEMA_VERSION = 1
TRACE_NAMESPACE = UUID("87272c71-f444-4e2d-9d89-c636d237f770")
MAX_ATTRIBUTE_TEXT = 2_000


class RunKind(StrEnum):
    ASK = "ASK"
    TASK = "TASK"
    WORKFLOW = "WORKFLOW"
    SCHEDULE = "SCHEDULE"


class SpanKind(StrEnum):
    REQUEST = "REQUEST"
    OBJECTIVE = "OBJECTIVE"
    PLAN = "PLAN"
    TASK = "TASK"
    ASSIGNMENT = "ASSIGNMENT"
    MODEL = "MODEL"
    TOOL = "TOOL"
    APPROVAL = "APPROVAL"
    ARTIFACT = "ARTIFACT"
    RETRY = "RETRY"
    REPLAN = "REPLAN"
    REASSIGNMENT = "REASSIGNMENT"
    ESCALATION = "ESCALATION"
    RESUME = "RESUME"
    SCHEDULE = "SCHEDULE"
    WORKFLOW = "WORKFLOW"
    RECOVERY = "RECOVERY"
    STATE = "STATE"
    EXPORT = "EXPORT"


class SpanStatus(StrEnum):
    UNSET = "UNSET"
    RUNNING = "RUNNING"
    OK = "OK"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    DENIED = "DENIED"
    DEGRADED = "DEGRADED"


def stable_span_id(trace_id: UUID, kind: SpanKind, entity_id: str, occurrence: int = 0) -> UUID:
    """Give projected source rows the same span id on every read."""
    return uuid5(TRACE_NAMESPACE, f"{trace_id}:{kind.value}:{entity_id}:{occurrence}")


def _bounded(value: Any) -> Any:
    safe = redact(value)
    if isinstance(safe, str):
        return safe if len(safe) <= MAX_ATTRIBUTE_TEXT else safe[:MAX_ATTRIBUTE_TEXT] + "..."
    if isinstance(safe, dict):
        return {str(key): _bounded(item) for key, item in safe.items()}
    if isinstance(safe, (list, tuple)):
        return [_bounded(item) for item in safe]
    return safe


@dataclass(frozen=True, slots=True)
class TraceEvent:
    trace_id: UUID
    span_id: UUID
    kind: SpanKind
    status: SpanStatus
    name: str
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    event_id: UUID = field(default_factory=uuid4)
    parent_id: UUID | None = None
    correlation_id: str = ""
    causation_id: UUID | None = None
    entity_type: str = ""
    entity_id: str = ""
    actor: str = ""
    reason_code: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    duration_ms: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    schema_version: int = TRACE_SCHEMA_VERSION

    def sanitized(self) -> TraceEvent:
        return replace(
            self,
            name=str(_bounded(self.name)),
            actor=str(_bounded(self.actor)),
            reason_code=str(_bounded(self.reason_code)),
            attributes=_bounded(self.attributes),
        )


@dataclass(frozen=True, slots=True)
class TraceView:
    trace_id: UUID
    run_kind: RunKind
    workspace_id: WorkspaceId
    root_entity_type: str
    root_entity_id: str
    events: tuple[TraceEvent, ...]
    schema_version: int = TRACE_SCHEMA_VERSION
    degraded: bool = False
    degradation_reason: str = ""

    @property
    def first_failure(self) -> TraceEvent | None:
        return next(
            (
                event
                for event in self.events
                if event.status in {SpanStatus.ERROR, SpanStatus.DENIED}
            ),
            None,
        )
