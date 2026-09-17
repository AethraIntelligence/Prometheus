"""One execution of a workflow, and where it is written down.

A run is recorded before its first step and updated as it goes, for the same
reason a task is: what was started has to be answerable after the process that
started it is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from domain.workflows.definition import WorkflowTrigger
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What became of one step. Kept in the run's result rather than as rows.

    A step is a task and the task table already holds it; this is the index -
    which task belonged to which step, and how many attempts it took.
    """

    step: str
    employee: str
    task_id: UUID | None = None
    succeeded: bool = False
    attempts: int = 0
    summary: str = ""
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "employee": self.employee,
            "task_id": str(self.task_id) if self.task_id else None,
            "succeeded": self.succeeded,
            "attempts": self.attempts,
            "summary": self.summary,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: UUID
    workflow: str
    workflow_version: int = 1
    trigger: WorkflowTrigger = WorkflowTrigger.MANUAL
    inputs: dict[str, Any] = field(default_factory=dict)
    status: RunStatus = RunStatus.RUNNING
    steps: tuple[StepOutcome, ...] = ()
    summary: str = ""
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def cost_usd(self) -> float:
        return round(sum(step.cost_usd for step in self.steps), 6)

    @property
    def quality(self) -> float:
        if not self.steps:
            return 0.0
        return round(sum(step.succeeded for step in self.steps) / len(self.steps), 4)

    @classmethod
    def create(cls, workflow: str, **extra: Any) -> WorkflowRun:
        return cls(id=uuid4(), workflow=workflow, **extra)

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.COMPLETED

    def with_step(self, outcome: StepOutcome) -> WorkflowRun:
        return replace(self, steps=(*self.steps, outcome))

    def finished(self, status: RunStatus, summary: str = "") -> WorkflowRun:
        return replace(
            self,
            status=status,
            summary=summary,
            finished_at=datetime.now(UTC),
        )

    def to_result(self) -> dict[str, Any]:
        """The stored shape of what happened, readable without the domain."""
        return {
            "summary": self.summary,
            "steps": [step.to_dict() for step in self.steps],
            "workflow_version": self.workflow_version,
            "cost_usd": self.cost_usd,
            "quality": self.quality,
        }


class WorkflowRunRepository(Protocol):
    async def save(self, run: WorkflowRun) -> None: ...

    async def get(self, run_id: UUID) -> WorkflowRun | None: ...

    async def recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 20
    ) -> list[WorkflowRun]: ...
