"""What Prometheus hands down, and what comes back."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from domain.employees.definition import EmployeeId
from domain.policies.models import ActorKind
from domain.tasks.task import TaskResult
from domain.workforce.acceptance import Acceptance
from domain.workforce.decision import Decision
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class AssignmentOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REASSIGNED = "REASSIGNED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class SharedContext:
    """What the delegator deliberately passed down.

    Not the whole conversation: an assignment carries what the executor needs,
    which is a decision the manager makes rather than a dump it forwards.
    """

    facts: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskAssignment:
    id: UUID
    task_id: UUID
    employee_id: EmployeeId
    assigned_by: ActorKind
    context: SharedContext = field(default_factory=SharedContext)
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    assigned_by_id: str | None = None
    assigned_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    accepted_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: AssignmentOutcome | None = None
    result: TaskResult | None = None
    #: Why this employee and not the others (Phase 11). None on assignments
    #: written before decisions were recorded, and on ones this build cannot read.
    decision: Decision | None = None
    #: What the manager made of the result on the evidence. None until judged,
    #: and on every assignment written before verdicts were kept.
    acceptance: Acceptance | None = None

    @classmethod
    def create(
        cls,
        task_id: UUID,
        employee_id: EmployeeId,
        assigned_by: ActorKind,
        **extra: Any,
    ) -> TaskAssignment:
        return cls(
            id=uuid4(),
            task_id=task_id,
            employee_id=employee_id,
            assigned_by=assigned_by,
            **extra,
        )

    def accept(self) -> TaskAssignment:
        return replace(self, accepted_at=datetime.now(UTC))

    def judged(self, acceptance: Acceptance) -> TaskAssignment:
        return replace(self, acceptance=acceptance)

    def close(self, outcome: AssignmentOutcome, result: TaskResult | None = None) -> TaskAssignment:
        return replace(
            self, outcome=outcome, result=result, completed_at=datetime.now(UTC)
        )
