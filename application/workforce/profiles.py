"""A role as a person reads it: its profile, its readiness and its record.

Every figure comes from something the platform recorded - assignments, the tasks
they produced, the approvals those tasks raised, validation runs - and none is
stored here. The facade calls this; an interface renders what comes back.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from application.workforce.readiness import ReadinessService
from domain.approvals.protocols import ApprovalRepository
from domain.employees.definition import EmployeeDefinition
from domain.employees.protocols import EmployeeRegistry
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task
from domain.validation.run import ValidationRunRepository
from domain.workforce import performance
from domain.workforce.assignment import TaskAssignment
from domain.workforce.performance import AssignmentFact, Performance
from domain.workforce.profile import EmployeeProfile, build, indistinguishable
from domain.workforce.readiness import assess
from domain.workforce.repository import AssignmentRepository
from domain.workspace.models import WorkspaceId

log = structlog.get_logger(__name__)

#: How much history one profile reads. A statistic over more than this in a
#: thirty-day window is a machine running a service, not a person's workforce,
#: and the reading is capped rather than left to grow with the store.
MAX_ASSIGNMENTS = 500
RECENT = 10


@dataclass(frozen=True, slots=True)
class RecentAssignment:
    assignment: TaskAssignment
    task: Task | None


@dataclass(frozen=True, slots=True)
class EmployeeRecord:
    profile: EmployeeProfile
    performance: Performance
    recent: tuple[RecentAssignment, ...]


class WorkforceProfiles:
    def __init__(
        self,
        *,
        registry: EmployeeRegistry,
        readiness: ReadinessService,
        assignments: AssignmentRepository,
        tasks: TaskRepository,
        approvals: ApprovalRepository | None = None,
        validation_runs: ValidationRunRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._registry = registry
        self._readiness = readiness
        self._assignments = assignments
        self._tasks = tasks
        self._approvals = approvals
        self._runs = validation_runs
        self._clock = clock

    def profiles(self, workspace_id: WorkspaceId) -> list[EmployeeProfile]:
        return [self._profile(definition) for definition in self._registry.list(workspace_id)]

    def overlaps(self, workspace_id: WorkspaceId) -> list[tuple[str, ...]]:
        return indistinguishable(self._registry.list(workspace_id))

    async def record(
        self,
        name: str,
        workspace_id: WorkspaceId,
        *,
        window: timedelta = performance.DEFAULT_WINDOW,
    ) -> EmployeeRecord | None:
        definition = next(
            (item for item in self._registry.list(workspace_id) if item.name == name), None
        )
        if definition is None:
            return None
        now = self._clock()
        made = await self._assignments.for_employee(
            definition.id,
            workspace_id=workspace_id,
            since=now - window,
            limit=MAX_ASSIGNMENTS,
        )
        facts: list[AssignmentFact] = []
        recent: list[RecentAssignment] = []
        for assignment in made:
            task = await self._tasks.get(assignment.task_id)
            if len(recent) < RECENT:
                recent.append(RecentAssignment(assignment, task))
            if task is None:
                continue
            facts.append(
                AssignmentFact(
                    task=task,
                    assigned_at=assignment.assigned_at,
                    closed_at=assignment.completed_at,
                    acceptance=assignment.acceptance,
                    approvals=await self._approvals_for(task.id),
                )
            )
        return EmployeeRecord(
            profile=self._profile(definition),
            performance=performance.measure(
                facts,
                now=now,
                window=window,
                scenario_runs=await self._scenario_runs(definition, workspace_id, now - window),
            ),
            recent=tuple(recent),
        )

    def _profile(self, definition: EmployeeDefinition) -> EmployeeProfile:
        facts = self._readiness.facts(definition)
        return build(definition, facts, assess(definition, facts))

    async def _approvals_for(self, task_id: UUID) -> int:
        if self._approvals is None:
            return 0
        return len(await self._approvals.for_task(task_id))

    async def _scenario_runs(
        self, definition: EmployeeDefinition, workspace_id: WorkspaceId, since: datetime
    ) -> list[bool]:
        if self._runs is None:
            return []
        try:
            runs = await self._runs.recent(workspace_id, limit=MAX_ASSIGNMENTS)
        except Exception as error:  # a missing validation store is no data, not a failure
            log.info("workforce.validation_runs_unavailable", error=str(error))
            return []
        return [
            run.passed
            for run in runs
            if str(definition.id) in run.employee_ids and run.started_at >= since
        ]
