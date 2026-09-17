"""SQLAlchemy models, written natively for SQLite.

UUIDs and timestamps are TEXT, JSON is TEXT read through SQLite's JSON
functions, and enums are TEXT with a CHECK constraint. Portability is provided
by the Protocols in `domain/`, not by a dialect-agnostic subset of SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

from domain.approvals.models import ApprovalGrant, ApprovalState
from domain.integrations.models import IntegrationKind, IntegrationStatus
from domain.knowledge.models import DocumentStatus
from domain.memory.models import MemoryKind, MemoryScope
from domain.policies.models import ActorKind
from domain.tasks.task import TaskStatus
from domain.validation.run import RunStatus as ValidationStatus
from domain.workflows.definition import WorkflowTrigger
from domain.workforce.protocols import ObjectiveStatus, PlanStatus
from infrastructure.persistence import knowledge_fts, memory_fts
from infrastructure.persistence.dialect import UtcTimestamp

TASK_STATUS_VALUES = tuple(status.value for status in TaskStatus)
APPROVAL_STATE_VALUES = tuple(state.value for state in ApprovalState)
APPROVAL_GRANT_VALUES = tuple(grant.value for grant in ApprovalGrant)
OBJECTIVE_STATUS_VALUES = tuple(status.value for status in ObjectiveStatus)
PLAN_STATUS_VALUES = tuple(status.value for status in PlanStatus)
MEMORY_SCOPE_VALUES = tuple(scope.value for scope in MemoryScope)
MEMORY_KIND_VALUES = tuple(kind.value for kind in MemoryKind)
ACTOR_KIND_VALUES = tuple(kind.value for kind in ActorKind)
AUDIT_RESULT_VALUES = ("SUCCESS", "FAILURE", "DENIED")
WORKFLOW_TRIGGER_VALUES = tuple(trigger.value for trigger in WorkflowTrigger)
WORKFLOW_STATUS_VALUES = ("RUNNING", "COMPLETED", "FAILED", "CANCELLED")
VALIDATION_STATUS_VALUES = tuple(status.value for status in ValidationStatus)
INTEGRATION_KIND_VALUES = tuple(kind.value for kind in IntegrationKind)
INTEGRATION_STATUS_VALUES = tuple(status.value for status in IntegrationStatus)
DOCUMENT_STATUS_VALUES = tuple(status.value for status in DocumentStatus)


class Base(DeclarativeBase):
    # Every `Mapped[datetime]` below is a moment in UTC, and gets the column type
    # that says so once (`dialect.UtcTimestamp`) rather than forty-three times.
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: UtcTimestamp}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EmployeeRow(Base):
    """The loaded version of an employee declaration.

    The source of truth stays the file under `employees/`; this table holds what
    was loaded plus any local override.
    """

    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_employees_workspace_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(256), nullable=False)
    role_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    goals: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    policies: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    allowed_tools: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    model_profile: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    memory_scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default="EMPLOYEE_PRIVATE"
    )
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(TASK_STATUS_VALUES) + "')",
            name="ck_tasks_status",
        ),
        Index("ix_tasks_workspace_status", "workspace_id", "status", "priority", "created_at"),
        Index("ix_tasks_parent_id", "parent_id"),
        Index("ix_tasks_plan_id", "plan_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    created_by: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    # No FK yet: plans arrive in Phase 7.
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskStatus.CREATED.value
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    assigned_employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id"), nullable=True
    )
    assignment_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: Step cursor and scratch state, saved after every step so a task survives
    #: a restart.
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class TaskEventRow(Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_task_id", "task_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class LLMCallRow(Base):
    """Telemetry for every model call.

    Recorded from the first call rather than added later: local development pays
    for each token, and a looping agent gets expensive before it gets wrong.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_task_id", "task_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[bool] = mapped_column(nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class TaskAssignmentRow(Base):
    """Who was asked to do what, and on whose authority.

    Kept separate from `tasks` because a task can be assigned more than once -
    reassigned after a failure, retried, or handed to a different employee - and
    the history of that is what makes a run legible afterwards.
    """

    __tablename__ = "task_assignments"
    __table_args__ = (
        Index("ix_task_assignments_task", "task_id", "assigned_at"),
        Index("ix_task_assignments_employee", "employee_id", "assigned_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("employees.id"), nullable=False
    )
    assigned_by: Mapped[str] = mapped_column(String(32), nullable=False)
    assigned_by_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: What the delegator deliberately passed down - not the whole conversation.
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    assigned_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class ToolCallRow(Base):
    """Every tool call, with its arguments redacted.

    Model calls have been accounted for since Phase 2; tools need the same
    treatment for the same reason. A failing loop does not announce itself - it
    just keeps calling, and this is where that becomes visible after the fact.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_task_id", "task_id", "id"),
        UniqueConstraint("task_id", "call_id", name="uq_tool_calls_task_call"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    success: Mapped[bool] = mapped_column(nullable=False, default=True)
    completed: Mapped[bool] = mapped_column(nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Which level of the interface hierarchy the call went through. Stored so
    #: "why did it drive a screen rather than call something" is answerable from
    #: the trace months later, without re-deriving it from a tool declaration
    #: that may since have changed.
    interface: Mapped[str] = mapped_column(String(16), nullable=False, default="API")
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class ApprovalRow(Base):
    """A question put to a human, and the answer.

    Written before the answer arrives, so a process killed mid-question leaves a
    PENDING row rather than an action nobody can account for.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "state IN ('" + "','".join(APPROVAL_STATE_VALUES) + "')",
            name="ck_approvals_state",
        ),
        Index("ix_approvals_state", "state", "requested_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    #: The tool the question is about, beside the rendered line a person reads.
    tool: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    resource: Mapped[str] = mapped_column(Text, nullable=False, default="")
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    policy_source: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    requires_explicit_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    context_sources: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    requested_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    #: When the question stops being worth answering. NULL waits forever.
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    grant_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ApprovalGrant.ONCE.value
    )
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class CapabilityLeaseRow(Base):
    """Exact, revocable authority created by an explicit approval."""

    __tablename__ = "capability_leases"
    __table_args__ = (
        CheckConstraint(
            "grant_kind IN ('TASK','PERSISTENT')",
            name="ck_capability_leases_grant_kind",
        ),
        Index("ix_capability_leases_active", "workspace_id", "revoked_at", "expires_at"),
        Index("ix_capability_leases_match", "workspace_id", "subject", "action", "resource"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    grant_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    approval_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approvals.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ObjectiveRow(Base):
    """What the user asked Prometheus for, and what became of it.

    The user's own sentence is `text` and is never rewritten. What Prometheus read out
    of it - the constraints, what would count as done - is stored beside it, so
    a misreading stays visible next to the thing it misread.
    """

    __tablename__ = "objectives"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(OBJECTIVE_STATUS_VALUES) + "')",
            name="ck_objectives_status",
        ),
        Index("ix_objectives_workspace_created", "workspace_id", "created_at"),
        Index("ix_objectives_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    #: The thread this was asked in, if any. No foreign key: deleting a thread
    #: must not delete the record of what was done because of it.
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    acceptance_criteria: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    #: `{"approvals": ..., "model": ...}` as the request was asked. Empty for a
    #: request from before there were any, which reads as the defaults.
    directions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ConversationRow(Base):
    """A thread of requests, and nothing else.

    Deliberately without a `messages` table beside it. One user message is one
    objective, already recorded in full; a second copy of the same history
    would be the one that goes stale. See migration 012.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_workspace_updated", "workspace_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="TASK")
    folder: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    approvals: Mapped[str | None] = mapped_column(String(8), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class PlanRow(Base):
    """One revision of Prometheus's decomposition of an objective.

    Superseded plans are kept. What the manager thought on the first attempt is
    the only evidence of why a second was needed.
    """

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(PLAN_STATUS_VALUES) + "')",
            name="ck_plans_status",
        ),
        UniqueConstraint("objective_id", "revision", name="uq_plans_objective_revision"),
        Index("ix_plans_objective", "objective_id", "revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    objective_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("objectives.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: The immutable tasks the manager proposed and their routing needs. Task
    #: rows replace these values as work starts, but an unstarted task must
    #: survive a crash between planning and delegation.
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class PlanTaskDependencyRow(Base):
    """One edge: this task cannot start until that one is done.

    Edges rather than an ordered list, because an order is a claim nobody
    checked. Edges can be checked for a cycle, and they say which tasks could
    run at the same time - which is what Phase 12 needs and what a list loses.

    The task columns carry no foreign key. A plan is recorded when Prometheus proposes
    it, and a task becomes a row when somebody is given it, so the edges legally
    precede both ends they point at. See migration 006.
    """

    __tablename__ = "plan_task_dependencies"

    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    depends_on: Mapped[str] = mapped_column(String(36), primary_key=True)


class MemoryItemRow(Base):
    """One thing worth remembering, and who is allowed to remember it.

    `scope` is an access boundary rather than a label, so it is stored beside
    the owner it names: an EMPLOYEE_PRIVATE row without an `employee_id` would
    be private to nobody. The text search over `content` is an FTS5 virtual
    table kept in step by triggers - see `memory_fts` - and it is reached only
    through the memory adapter, never from a caller.

    `employee_id` and `task_id` carry no foreign key. Memory is written *about*
    a task and outlives it: an employee taken out of `employees/` and a task
    deleted from history would otherwise take with them the record of what was
    learned from them, which is the one thing here worth keeping.
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('" + "','".join(MEMORY_SCOPE_VALUES) + "')",
            name="ck_memory_items_scope",
        ),
        CheckConstraint(
            "kind IN ('" + "','".join(MEMORY_KIND_VALUES) + "')",
            name="ck_memory_items_kind",
        ),
        Index("ix_memory_items_scope", "workspace_id", "scope", "kind", "created_at"),
        Index("ix_memory_items_employee", "employee_id", "kind", "created_at"),
        Index("ix_memory_items_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    employee_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # `metadata` is taken on a declarative class, so the attribute is renamed
    # and the column keeps the name the schema declares.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Provenance and revision, since migration 039. Columns rather than keys in
    # `metadata`, because recall filters on the status and a person sorts by
    # the confidence - and a key nobody is obliged to write is a key some
    # writer forgets.
    basis: Mapped[str] = mapped_column(String(16), nullable=False, default="INFERRED")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revised_at: Mapped[datetime | None] = mapped_column(nullable=True)
    contradicts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    source_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    derived_from: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class MemoryUseRow(Base):
    """Which memory a piece of work was given, and why (`domain/memory/usage.py`).

    No foreign keys and no copy of the content: a use outlives the memory it
    names, and forgetting a memory must not leave its words behind here.
    """

    __tablename__ = "memory_uses"
    __table_args__ = (
        Index("ix_memory_uses_objective", "objective_id", "used_at"),
        Index("ix_memory_uses_task", "task_id", "used_at"),
        Index("ix_memory_uses_memory", "memory_id", "used_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    memory_id: Mapped[str] = mapped_column(String(36), nullable=False)
    objective_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reader: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    used_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class ConversationSessionRow(Base):
    """The compacted half of a thread's brief (`domain/conversations/session.py`).

    One row per thread, and only what a model wrote: decisions, open questions
    and files are projected from `objectives` on every read and never copied
    here. No foreign key to `conversations`, like every label on this schema.
    """

    __tablename__ = "conversation_sessions"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    goal_brief: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    resolved_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class AuditRow(Base):
    """One action, who took it and what came of it.

    Separate from `tool_calls` on purpose: that table answers "what did this run
    cost and how long did it take", and this one answers "who did what here".
    The two overlap for a successful tool call and diverge exactly where it
    matters - a denied action has an audit line and no tool call, because the
    tool never ran, and those are the lines an audit exists for.

    No foreign keys. An audit outlives what it describes; keying it to `tasks`
    would mean clearing history erases the record of what was done, which is the
    one thing a record must survive.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "actor_kind IN ('" + "','".join(ACTOR_KIND_VALUES) + "')",
            name="ck_audit_log_actor_kind",
        ),
        CheckConstraint(
            "result IN ('" + "','".join(AUDIT_RESULT_VALUES) + "')",
            name="ck_audit_log_result",
        ),
        Index("ix_audit_log_ts", "ts"),
        Index("ix_audit_log_task", "task_id", "ts"),
        Index("ix_audit_log_actor", "actor_kind", "actor_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    assignment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class WorkflowRunRow(Base):
    """One execution of a predefined process."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('" + "','".join(WORKFLOW_TRIGGER_VALUES) + "')",
            name="ck_workflow_runs_trigger",
        ),
        CheckConstraint(
            "status IN ('" + "','".join(WORKFLOW_STATUS_VALUES) + "')",
            name="ck_workflow_runs_status",
        ),
        Index("ix_workflow_runs_started", "workspace_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    workflow: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ValidationRunRow(Base):
    """One attempt at one declared scenario.

    No foreign keys, and no column pointing at the tasks the run produced. The
    record has to outlive them: the question it answers - did this capability
    work here, and how often - is asked months later, by which time the tasks
    may well have been cleared. Same reasoning as `audit_log`.

    `failure` is a plain string rather than a constrained enum. The taxonomy is
    a working hypothesis about what goes wrong with this platform (§11.3) and is
    expected to gain a category the first time something goes wrong in a way
    nobody had a name for; a CHECK constraint would make that a migration, and a
    migration is exactly the wrong amount of friction for a finding.
    """

    __tablename__ = "validation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(VALIDATION_STATUS_VALUES) + "')",
            name="ck_validation_runs_status",
        ),
        Index("ix_validation_runs_scenario", "workspace_id", "scenario", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    scenario: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ScheduleRow(Base):
    """A standing instruction to ask Prometheus for something (§12.9).

    It holds a request in words, not a plan and not an employee: a schedule
    outlives the workforce it was written against, and one that named an
    employee would keep pointing at a declaration somebody deleted.

    `next_due_at` is stored rather than computed, which makes "what is due" an
    index lookup instead of a scan that re-derives a recurrence per row - and,
    more importantly, makes it survive a restart. A timer living in a process
    that died is a schedule that silently stopped.

    `last_objective_id` carries no foreign key. It is a pointer for somebody
    following what a schedule actually did, and clearing old objectives must
    not delete the standing instructions that produced them.
    """

    __tablename__ = "schedules"
    __table_args__ = (Index("ix_schedules_due", "workspace_id", "enabled", "next_due_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    request: Mapped[str] = mapped_column(Text, nullable=False)
    every_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_at: Mapped[str | None] = mapped_column(String(8), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    on_event: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_due_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_objective_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: The thread its firings are written into. No foreign key, like the column
    #: it mirrors on `objectives`: deleting a thread must not delete the
    #: standing instruction, and the reverse is true too.
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: The catalog entry its runs prefer, by name. Empty: the router decides.
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: ASK, AUTO or DENY - what its runs do about an action that needs approval.
    approvals: Mapped[str] = mapped_column(String(8), nullable=False, default="ASK")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    workflow_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    workflow_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow_inputs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    workflow_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    retry_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="DECLARED")
    misfire_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="COALESCE")
    concurrency_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="SKIP")
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class EventRow(Base):
    """Something that happened which work might be owed to.

    A row rather than a callback because an event has to survive arriving while
    nothing is listening - a process killed, a machine asleep, the scheduler
    started five minutes later. An in-process signal survives none of those, and
    is silent about it in every case.

    `consumed_at` lives here and not on the schedule: two schedules waiting on
    one kind of event is a legitimate configuration, each firing twice for one
    event is not, and recording the claim on the thing being claimed is what
    makes the second impossible rather than unlikely.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_pending", "workspace_id", "kind", "consumed_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class WorkspaceRow(Base):
    """The table the `workspace_id` column has been pointing at since 001.

    No foreign keys point here. Every other table already carries the column as
    a plain string, and turning thirteen of them into children of this row would
    mean that deleting a workspace either cascades through the whole history or
    is refused by the database - both of which are decisions for the application
    to make out loud rather than for the schema to make silently.
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class IntegrationRow(Base):
    """An external service the user connected.

    Three of these columns are the trust boundary, and all three are written on
    this machine rather than by the service (ADR 0015).

    `effects` maps a tool's own name to what it does to the world, and it is the
    only thing the policy layer reads - a server does not get a vote on whether
    its own actions need approval. `capabilities` is what an employee granted
    this integration can thereby be asked to do, in the platform's own closed
    vocabulary. `discovered` is what the server last said it offers, cached so
    that listing tools or checking a declaration never has to start it.

    `configuration` holds how to reach it and never holds a credential:
    `secret_names` says which secrets are needed, and the values are resolved at
    the moment of a call from wherever secrets actually live.

    Unique on (workspace, name) because the name is what an employee's
    declaration says and what every tool is prefixed with. Two integrations
    called gmail would make `gmail.send_message` ambiguous, which is a thing
    that must be impossible rather than merely discouraged.
    """

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_integrations_name"),
        CheckConstraint(
            "kind IN " + str(INTEGRATION_KIND_VALUES), name="ck_integrations_kind"
        ),
        CheckConstraint(
            "status IN " + str(INTEGRATION_STATUS_VALUES), name="ck_integrations_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="MCP")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCONNECTED")
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    effects: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    capabilities: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    secret_names: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    discovered: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    granted_to: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

class DocumentRow(Base):
    """Something the user brought: a contract, a specification, minutes.

    Deliberately not a `memory_items` row with a different kind. Memory decays
    and is pruned by age; a document the user uploaded does not stop being true
    because nobody opened it for a fortnight, and the maintenance that keeps
    memory bounded would quietly delete it (ADR 0016).

    `checksum` is of the extracted text rather than of the file, so re-adding a
    document that has not changed updates one record instead of making a second
    copy that answers the same queries twice.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN " + str(DOCUMENT_STATUS_VALUES), name="ck_documents_status"
        ),
        Index("ix_documents_workspace", "workspace_id", "created_at"),
        Index("ix_documents_checksum", "workspace_id", "checksum"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_type: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class ChunkRow(Base):
    """A passage of a document, with the model that embedded it beside it.

    `embedding_model` and `embedding_dimension` are the two columns that make a
    change of embedding model answerable instead of a guess: vectors written by
    another model are not comparable with a query's, and a name that matches
    while the length does not is exactly the case where comparing anyway
    produces a number rather than an error (ADR 0016).

    The vector is bytes - float32, in order - rather than JSON. It is read for
    every query and never read by a person, and a text encoding of a thousand
    floats is four times the file for no benefit. A store that has a vector type
    of its own uses it; this is the local one.

    The foreign key onto `documents` is the one place in this schema a cascade
    is right: a chunk is part of its document, not a record about it, and a
    chunk whose document is gone is unreachable text that still answers queries.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_ordinal"),
        Index("ix_chunks_document", "document_id", "ordinal"),
        Index("ix_chunks_workspace", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class ConnectionRow(Base):
    """A way in to a provider: which vendor, reached how, paying with what.

    Unique on (workspace, name) for the same reason integrations are: the name
    is what a catalog entry points at, and two rows answering to one name would
    make "which key does this model use" a question with two answers.

    `secret_name` names a credential and never holds one. The value lives in
    `secrets`, encrypted, and is resolved at the moment a client is built.
    """

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_connections_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    secret_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: Whether this kind can work without a credential. Stored rather than
    #: re-derived, so a row still reads correctly on a machine whose adapters
    #: have changed under it.
    needs_credential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class ModelEntryRow(Base):
    """One catalog entry: a model, and the connection it is reached through.

    The catalog was a TOML file and stays one on disk - the shipped file is what
    a fresh installation is seeded from, and `PROMETHEUS_MODEL_CATALOG_PATH` still
    overrides everything for a machine with no window. What this table adds is
    the entry a *person* added, which cannot live in a file inside an installed
    application.

    `capabilities` is a list of the platform's own capability names, not a
    provider's: what a model can do is the vocabulary the router filters on, and
    a vendor does not get a vote on it. Costs are per 1k tokens, as in the file.
    """

    __tablename__ = "model_entries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_model_entries_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    connection: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=8192)
    input_cost_per_1k_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    output_cost_per_1k_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class TaskDefaultRow(Base):
    """Which catalog entry a kind of work goes to.

    One row per kind of work, and that is the whole of "give this key to this
    task". Deliberately not per employee: an employee's declaration may not name
    a model (ADR 0003), and a table keyed by employee would be that rule broken
    somewhere the rule cannot see it.
    """

    __tablename__ = "task_defaults"

    task_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    entry_name: Mapped[str] = mapped_column(String(120), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


class SecretRow(Base):
    """A credential, encrypted, with the master key kept anywhere but here.

    In the store rather than in a file beside it because the backend has to be
    deployable: a container that restarts with an empty filesystem loses a file
    and keeps its database. What lands here is AES-GCM ciphertext, and the key
    that opens it comes from `PROMETHEUS_MASTER_KEY` or a 0600 file - so a backup,
    a dump, or `storage-migrate` carries something unreadable rather than a set
    of live API keys.

    The name is the identity, as it is for every other credential in the
    platform: an integration record and a provider connection both *name* the
    secret they need and never hold it.
    """

    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)


# The search index is part of the schema, not of the adapter: a database built
# by `create_all` - which is what the test suite does - has to be searchable the
# same way the migrated one is, or the tests exercise a different backend than
# the product ships.
# Both dialects, for the same reason: Phase 19 found `create_all` building a
# PostgreSQL schema with no text index at all, and the one test that searched
# there creating the index itself in its fixture - a harness quietly holding up
# the thing it was meant to be checking.
for _statement in (*memory_fts.CREATE, *knowledge_fts.CREATE):
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),
    )

for _statement in (*memory_fts.CREATE_POSTGRES, *knowledge_fts.CREATE_POSTGRES):
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_statement).execute_if(dialect="postgresql"),
    )
