"""Offering to keep a process the manager keeps rediscovering.

Detection runs when a person looks, not after every objective: the question is
asked by a screen, and a background job writing suggestions nobody opens would
be work nobody asked for. It is idempotent - the same store and the same clock
produce the same suggestions - so looking twice changes nothing.

What counts as a run of a process is decided from the record only: the
objective finished DONE, its final plan is DONE, and every step of it has a
completed task whose result was accepted on the evidence. Who did each step is
read off the task that delivered it, not off the plan's intent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from domain.employees.protocols import EmployeeRegistry
from domain.errors import NotFoundError, PrometheusError
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task, TaskStatus
from domain.workflows.definition import WorkflowDefinition
from domain.workflows.suggestion import (
    DEFAULT_WINDOW,
    StepShape,
    SuccessfulRun,
    Suggestion,
    SuggestionRepository,
    SuggestionStatus,
    WorkflowWriter,
    draft,
    patterns,
    reconcile,
)
from domain.workforce.acceptance import accept
from domain.workforce.protocols import ObjectiveStatus, Plan, PlanStatus
from domain.workforce.repository import (
    AssignmentRepository,
    ObjectiveRepository,
    PlanRepository,
)
from domain.workspace.models import WorkspaceId

log = structlog.get_logger(__name__)

#: How many recent objectives one look reads. Enough for a month of a person's
#: work; a pattern older than what fits here is not a habit worth a prompt.
SCANNED_OBJECTIVES = 300
MAX_SNOOZE = timedelta(days=90)


class SuggestionNotFoundError(NotFoundError):
    """No suggestion by that id in this workspace."""


@dataclass(frozen=True, slots=True)
class Shown:
    suggestion: Suggestion
    #: What the saved workflow would be, under a proposed name.
    draft: WorkflowDefinition


class WorkflowSuggestions:
    def __init__(
        self,
        *,
        suggestions: SuggestionRepository,
        objectives: ObjectiveRepository,
        plans: PlanRepository,
        tasks: TaskRepository,
        assignments: AssignmentRepository,
        registry: EmployeeRegistry,
        writer: WorkflowWriter | None = None,
        reload: Callable[[], None] = lambda: None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        window: timedelta = DEFAULT_WINDOW,
    ) -> None:
        self._suggestions = suggestions
        self._objectives = objectives
        self._plans = plans
        self._tasks = tasks
        self._assignments = assignments
        self._registry = registry
        self._writer = writer
        self._reload = reload
        self._clock = clock
        self._window = window

    async def refresh(self, workspace_id: WorkspaceId) -> list[Shown]:
        """Reconcile stored suggestions with the evidence, and return what to show."""
        now = self._clock()
        found = {
            pattern.fingerprint: pattern
            for pattern in patterns(
                await self._runs(workspace_id, now), now=now, window=self._window
            )
        }
        stored = {item.fingerprint: item for item in await self._suggestions.list(workspace_id)}
        for key in set(found) | set(stored):
            current = stored.get(key)
            updated = reconcile(current, found.get(key), workspace_id=workspace_id, now=now)
            if updated is not None and updated != current:
                await self._suggestions.save(updated)
                stored[key] = updated
        return [
            Shown(item, draft(item, proposed_name(item)))
            for item in sorted(stored.values(), key=lambda item: item.last_seen, reverse=True)
            if item.visible(now)
        ]

    async def dismiss(self, suggestion_id: UUID, workspace_id: WorkspaceId) -> Suggestion:
        item = await self._owned(suggestion_id, workspace_id)
        dismissed = item.dismissed(self._clock())
        await self._suggestions.save(dismissed)
        log.info("workflow_suggestion.dismissed", id=str(item.id), sources=item.occurrences)
        return dismissed

    async def snooze(
        self, suggestion_id: UUID, workspace_id: WorkspaceId, *, days: int
    ) -> Suggestion:
        if days < 1 or timedelta(days=days) > MAX_SNOOZE:
            raise PrometheusError(f"Snooze for between 1 and {MAX_SNOOZE.days} days.")
        item = await self._owned(suggestion_id, workspace_id)
        now = self._clock()
        snoozed = item.snoozed(now + timedelta(days=days), now)
        await self._suggestions.save(snoozed)
        return snoozed

    async def save(
        self,
        suggestion_id: UUID,
        workspace_id: WorkspaceId,
        *,
        name: str,
        description: str = "",
        version: int = 1,
    ) -> tuple[Suggestion, str]:
        """The person's confirmation: write the workflow, and nothing else.

        Refused unless the suggestion is currently shown - a dismissed or
        already-saved one is not a draft anybody confirmed. The file is written
        before the status changes, so a failed write leaves the suggestion open.

        `version` above 1 is an improvement on a process of that name already
        saved here: it is written beside the old one rather than over it, and
        nothing about the old one stops working. Which version anything *uses*
        is decided by whoever pinned it, not here.
        """
        if self._writer is None:
            raise PrometheusError("Workflows cannot be saved on this machine.")
        item = await self._owned(suggestion_id, workspace_id)
        if not item.visible(self._clock()):
            raise PrometheusError("This suggestion is not open; there is nothing to save.")
        definition = replace(draft(item, name.strip(), description.strip()), version=version)
        where = self._writer.write(definition)
        self._reload()
        saved = item.saved(definition.name, self._clock())
        await self._suggestions.save(saved)
        log.info(
            "workflow_suggestion.saved",
            id=str(item.id),
            workflow=definition.name,
            version=definition.version,
        )
        return saved, where

    async def _owned(self, suggestion_id: UUID, workspace_id: WorkspaceId) -> Suggestion:
        item = await self._suggestions.get(suggestion_id)
        if item is None or item.workspace_id != workspace_id:
            raise SuggestionNotFoundError(f"No workflow suggestion {suggestion_id} here.")
        return item

    async def _runs(self, workspace_id: WorkspaceId, now: datetime) -> list[SuccessfulRun]:
        names = {definition.id: definition.name for definition in self._registry.list(workspace_id)}
        runs: list[SuccessfulRun] = []
        for objective in await self._objectives.list_recent(
            workspace_id, limit=SCANNED_OBJECTIVES
        ):
            finished = objective.finished_at
            if (
                objective.status is not ObjectiveStatus.DONE
                or finished is None
                or finished < now - self._window
            ):
                continue
            plan = next(
                (
                    plan
                    for plan in await self._plans.for_objective(objective.id)
                    if plan.status is PlanStatus.DONE
                ),
                None,
            )
            if plan is None:
                continue
            steps = await self._shape(plan, names)
            if steps is not None:
                runs.append(SuccessfulRun(objective.id, finished, steps))
        return runs

    async def _shape(self, plan: Plan, names: dict[UUID, str]) -> tuple[StepShape, ...] | None:
        """The plan as structure, or None if any step lacks an accepted result."""
        in_plan = {task.id for task in plan.tasks}
        roots = [task for task in plan.tasks if task.parent_id not in in_plan]
        index = {task.id: position for position, task in enumerate(roots)}
        steps: list[StepShape] = []
        for root in roots:
            delivered = await self._delivered(root, plan)
            if delivered is None or delivered.assigned_employee_id not in names:
                return None
            requirement = plan.requirements.get(root.id)
            needs = (
                tuple(sorted(c.value for c in requirement.capabilities.required))
                + tuple(sorted(requirement.services))
                if requirement is not None
                else ()
            )
            steps.append(
                StepShape(
                    employee=names[delivered.assigned_employee_id],
                    needs=needs,
                    depends_on=tuple(
                        sorted(index[other] for other in plan.depends_on(root.id) if other in index)
                    ),
                )
            )
        return tuple(steps)

    async def _delivered(self, root: Task, plan: Plan) -> Task | None:
        attempts = [
            task
            for task in plan.tasks
            if (task.id == root.id or task.parent_id == root.id)
            and task.status is TaskStatus.COMPLETED
        ]
        for task in sorted(attempts, key=lambda item: item.updated_at, reverse=True):
            made = await self._assignments.for_task(task.id)
            verdict = made[0].acceptance if made and made[0].acceptance else accept(task)
            if verdict.accepted:
                return task
        return None


def proposed_name(suggestion: Suggestion) -> str:
    """A name built from who does the work, never from what anybody asked."""
    people: list[str] = []
    for step in suggestion.steps:
        if step.employee not in people:
            people.append(step.employee)
    base = "-then-".join(people)[:48].strip("-") or "process"
    return f"{base}-{suggestion.fingerprint[:6]}"


__all__ = [
    "Shown",
    "SuggestionNotFoundError",
    "SuggestionStatus",
    "WorkflowSuggestions",
    "proposed_name",
]
