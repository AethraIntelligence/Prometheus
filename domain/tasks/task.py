"""The unit of work every employee executes and every manager tracks."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from domain.errors import InvalidStateTransitionError
from domain.tasks.plan import TaskPlan
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_FOR_TOOL = "WAITING_FOR_TOOL"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PAUSED = "PAUSED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

#: Statuses a task can be picked up from after a restart. A task parked on a
#: tool or an approval is waiting on the outside world, not on us.
RESUMABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.CREATED,
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_FOR_TOOL,
        TaskStatus.VERIFYING,
    }
)

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    # FAILED is reachable from CREATED because a task can fail before it starts -
    # its employee no longer exists, the provider is unconfigured - and burying
    # that behind an illegal-transition error would hide the real cause.
    TaskStatus.CREATED: frozenset(
        {TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PLANNING: frozenset({TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_FOR_TOOL,
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.VERIFYING,
            TaskStatus.PLANNING,
            TaskStatus.PAUSED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_TOOL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    # A rejected result goes back to planning, not straight back to work: the
    # second attempt has to be better informed than the first, and that is a
    # decision about the plan.
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.PAUSED,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS[from_status]


class TaskCreatedBy(StrEnum):
    USER = "user"
    PROMETHEUS = "prometheus"
    SCHEDULE = "schedule"
    WORKFLOW = "workflow"


@dataclass(frozen=True, slots=True)
class TaskResult:
    """What the task produced, once it reached a terminal status."""

    summary: str
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskError:
    kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Execution:
    """The cursor a task resumes from after a restart.

    Persisted after every step, so an interrupted run picks up where it stopped
    instead of starting over.
    """

    step: int = 0
    state: dict[str, Any] = field(default_factory=dict)

    def advance(self, **state: Any) -> Execution:
        return Execution(step=self.step + 1, state={**self.state, **state})


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """One recorded status transition."""

    task_id: UUID
    from_status: TaskStatus | None
    to_status: TaskStatus
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class Task:
    """A goal, its state machine, and everything needed to resume it."""

    id: UUID
    workspace_id: WorkspaceId
    goal: str
    status: TaskStatus = TaskStatus.CREATED
    created_by: TaskCreatedBy = TaskCreatedBy.USER
    priority: int = 5
    parent_id: UUID | None = None
    plan_id: UUID | None = None
    workflow_run_id: UUID | None = None
    assigned_employee_id: UUID | None = None
    assignment_reason: str = ""
    plan: TaskPlan | None = None
    execution: Execution = field(default_factory=Execution)
    result: TaskResult | None = None
    error: TaskError | None = None
    attempts: int = 0
    cost_usd: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        goal: str,
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        created_by: TaskCreatedBy = TaskCreatedBy.USER,
        priority: int = 5,
        parent_id: UUID | None = None,
    ) -> Task:
        return cls(
            id=uuid4(),
            workspace_id=workspace_id,
            goal=goal,
            created_by=created_by,
            priority=priority,
            parent_id=parent_id,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_resumable(self) -> bool:
        return self.status in RESUMABLE_STATUSES or self.status is TaskStatus.PAUSED

    def transition_to(self, status: TaskStatus, **changes: Any) -> tuple[Task, TaskEvent]:
        """Move to `status`, returning the new task and the event that records it.

        Tasks are immutable values: callers persist the returned task rather than
        mutating this one, which keeps the event and the state in step.
        """
        if not can_transition(self.status, status):
            raise InvalidStateTransitionError(self.status, status)
        updated = replace(self, status=status, updated_at=datetime.now(UTC), **changes)
        event = TaskEvent(task_id=self.id, from_status=self.status, to_status=status)
        return updated, event

    def with_execution(self, execution: Execution) -> Task:
        return replace(self, execution=execution, updated_at=datetime.now(UTC))

    def complete(self, result: TaskResult) -> tuple[Task, TaskEvent]:
        return self.transition_to(TaskStatus.COMPLETED, result=result)

    def fail(self, error: TaskError) -> tuple[Task, TaskEvent]:
        return self.transition_to(TaskStatus.FAILED, error=error, attempts=self.attempts + 1)
