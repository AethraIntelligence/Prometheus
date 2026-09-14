"""The manager's contract.

Declared in Phase 1 so everything below it is written against a fixed shape;
implemented in Phase 7, once there is a dependable executor to manage.

The values here are the manager's vocabulary and nothing more. There is no
employee named anywhere in this file, and no way to name one: Prometheus reaches the
workforce through `EmployeeRegistry` and reaches the work through
`TaskExecution`, both of which it is handed.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from domain.tasks.task import Task
from domain.workforce.assignment import TaskAssignment
from domain.workforce.directions import NONE as NO_DIRECTIONS
from domain.workforce.directions import Directions
from domain.workforce.routing import Requirement
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class ObjectiveStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    #: A person stopped it. Not FAILED: nothing went wrong, and a thread that
    #: read "Failed" under every request somebody changed their mind about
    #: would teach them the word means nothing.
    CANCELLED = "CANCELLED"


#: Statuses an objective cannot move out of. An objective that has been
#: escalated is finished as far as Prometheus is concerned: it is now the user's.
TERMINAL_OBJECTIVE_STATUSES: frozenset[ObjectiveStatus] = frozenset(
    {
        ObjectiveStatus.DONE,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.ESCALATED,
        ObjectiveStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class Objective:
    """What the user asked for, in their own words - and what that turned out to mean.

    `text` is kept verbatim. Everything read out of it - the constraints, what
    would count as done - is stored beside it rather than replacing it, because
    a misreading has to stay visible next to the sentence it came from.
    """

    id: UUID
    text: str
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    constraints: dict[str, Any] = field(default_factory=dict)
    #: What the user would have to see to call this done. Written down before
    #: the work starts so the verdict at the end is not judged against a
    #: standard invented after seeing the result.
    acceptance_criteria: tuple[str, ...] = ()
    status: ObjectiveStatus = ObjectiveStatus.RECEIVED
    result: ObjectiveResult | None = None
    #: The thread this was asked in, when it was asked through an interface
    #: that keeps one. None is not an error and never will be: the CLI, a
    #: schedule and an event all state a goal with no conversation around it,
    #: and an objective is complete without one.
    conversation_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    #: How it was asked to be carried out - approvals and a preferred model.
    #: Recorded, not obeyed from here: the run reads them from its context.
    #: Kept so a thread reopened tomorrow shows what its last request was set
    #: to, instead of a composer that forgot and says "Auto".
    directions: Directions = NO_DIRECTIONS

    @classmethod
    def create(cls, text: str, **extra: Any) -> Objective:
        return cls(id=uuid4(), text=text, **extra)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_OBJECTIVE_STATUSES

    def to(self, status: ObjectiveStatus, result: ObjectiveResult | None = None) -> Objective:
        """Move on, stamping the finish time when there is one.

        Deliberately not a state machine with a transition table like `Task`.
        A task is resumed by a loop that has to know what it may do next; an
        objective is driven from one place, and a table here would be ceremony.
        """
        finished = (
            datetime.now(UTC) if status in TERMINAL_OBJECTIVE_STATUSES else self.finished_at
        )
        return replace(
            self, status=status, result=result or self.result, finished_at=finished
        )


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True, slots=True)
class Plan:
    """A decomposition of one objective into tasks, plus their dependencies.

    Replanning produces a *new* plan at the next revision and supersedes this
    one rather than editing it. What Prometheus thought at the first attempt is the
    only evidence of why the second was needed, and an edited plan destroys it.
    """

    id: UUID
    objective_id: UUID
    tasks: tuple[Task, ...] = ()
    dependencies: tuple[tuple[UUID, UUID], ...] = ()  # (task_id, depends_on)
    #: What each task needs from whoever takes it, by task id - what they must
    #: be able to do, and which connected service they must hold. A routing
    #: hint, and deliberately not persisted: the plan's shape is a record, but
    #: what it took to choose an employee is an input to a decision already
    #: recorded on the assignment. A plan read back from storage has none, which
    #: is correct - nothing re-delegates a plan that has already run.
    requirements: dict[UUID, Requirement] = field(default_factory=dict)
    revision: int = 1
    status: PlanStatus = PlanStatus.DRAFT
    rationale: str = ""
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID

    @classmethod
    def create(cls, objective_id: UUID, **extra: Any) -> Plan:
        return cls(id=uuid4(), objective_id=objective_id, **extra)

    def to(self, status: PlanStatus) -> Plan:
        return replace(self, status=status)

    def superseded(self) -> Plan:
        return replace(self, status=PlanStatus.SUPERSEDED)

    def depends_on(self, task_id: UUID) -> frozenset[UUID]:
        return frozenset(other for task, other in self.dependencies if task == task_id)

    def ready(self, done: Container[UUID]) -> tuple[Task, ...]:
        """The tasks whose dependencies are all satisfied, in plan order.

        Returning several is what makes Phase 12's parallelism a change of
        executor rather than a change of plan: today they are run one after
        another, and nothing about the plan says they have to be.
        """
        return tuple(
            task
            for task in self.tasks
            if task.id not in done and all(other in done for other in self.depends_on(task.id))
        )

    def blocked(self, done: Container[UUID]) -> bool:
        """Nothing left to run, and unfinished tasks that will never be ready.

        A cycle in the dependencies looks exactly like this, and so does a task
        waiting on one that failed. Both mean the plan is finished with work
        outstanding, which is a fact the supervisor has to be told rather than
        left to discover by looping.
        """
        outstanding = [task for task in self.tasks if task.id not in done]
        return bool(outstanding) and not self.ready(done)


@dataclass(frozen=True, slots=True)
class PlanProgress:
    plan_id: UUID
    total: int
    completed: int
    failed: int
    running: int

    @property
    def is_finished(self) -> bool:
        return self.completed + self.failed == self.total


@dataclass(frozen=True, slots=True)
class ObjectiveResult:
    """One answer to the user, assembled from whatever it took to get there.

    `summary` is the whole of what the user reads. Everything else is the
    evidence behind it: which tasks ran, which employee did each, what each one
    produced, and - when the answer is not what was asked for - what is missing
    and why. An answer nobody can check is the failure mode this exists against.
    """

    objective_id: UUID
    summary: str
    status: ObjectiveStatus
    output: dict[str, Any] = field(default_factory=dict)
    #: What the objective's acceptance criteria still do not have. Empty on a
    #: DONE result, and the reason for escalating on one that is not.
    missing: tuple[str, ...] = ()
    cost_usd: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status is ObjectiveStatus.DONE


class WorkforceManager(Protocol):
    """Prometheus's contract: the user's single entry point into the workforce."""

    async def receive(
        self,
        request: str,
        workspace_id: WorkspaceId | None = None,
        conversation_id: UUID | None = None,
    ) -> Objective:
        """Write down what was asked, before anything is done about it.

        Part of the contract rather than an implementation detail because a
        caller that is not a person needs it: the scheduler has a sentence and
        no conversation, and an objective row is the only thing that will
        explain, afterwards, why a run started at four in the morning.
        """
        ...

    async def handle_objective(self, objective: Objective) -> ObjectiveResult: ...

    async def plan(self, objective: Objective) -> Plan: ...

    async def delegate(self, task: Task) -> TaskAssignment: ...

    async def supervise(self, plan_id: UUID) -> PlanProgress: ...

    async def synthesize(self, plan_id: UUID) -> ObjectiveResult: ...


class Delegator(Protocol):
    """Picks the employee for a task and produces the assignment."""

    async def delegate(self, task: Task) -> TaskAssignment: ...


class TaskExecution(Protocol):
    """How the manager gets a task actually done.

    Prometheus does not run tasks; it decides which ones exist, who should do each and
    whether what came back is good enough. Stating that as a contract rather
    than an import is what keeps `application/prometheus/` free of the runtime - and
    what lets the whole manager be tested against a stand-in that never calls a
    model (Phase 7 DoD).

    One method, and it takes a task that already exists. The manager composes
    the task and picks the employee; handing over a goal and a name instead
    would mean the executor created the task, and the plan's dependency edges
    would then point at ids nobody had yet.
    """

    async def start(self, task: Task, assignment: TaskAssignment) -> Task:
        """Persist both, carry the task to a terminal state, and return it."""
        ...
