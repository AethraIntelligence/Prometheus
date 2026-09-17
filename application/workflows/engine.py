"""Running a process somebody already knows the shape of.

Prometheus plans an objective it has never seen before. A workflow is the opposite
case - the decomposition is known, was written down, and should not be
rediscovered by a model every Monday. What must *not* differ is everything
below the decomposition: the same employees, the same runtime, the same tool
registry, the same approval gate. So this engine decides nothing about how work
is done; it walks a declared graph and hands each step to `StepExecution`, which
is the contract the manager uses for exactly the same purpose.

Three things are worth stating, and each is a failure mode the design avoids.

**Dependencies are declared edges, resolved before anything runs.** A cycle is
reported as a cycle, not discovered by a run that never terminates - the same
rule Phase 7 applied to plans, for the same reason.

**A step that fails takes the run down by default.** A later step usually reads
what an earlier one produced, and running it anyway produces a confident answer
built on a gap. `on_failure: CONTINUE` is available and is a decision the author
of the workflow makes per step, in the file, where a reader can see it.

**Retries are per step and re-enter the whole step.** The runner already retries
a transient provider failure inside one task; what this adds is the case where
the task itself came back unsuccessful. Whether that is worth repeating depends
on what the step does, which is why the number lives on the step.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime

import structlog

from domain.errors import ConfigurationError
from domain.tasks.task import Task, TaskCreatedBy, TaskStatus
from domain.workflows.definition import (
    OnFailure,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowTrigger,
)
from domain.workflows.protocols import StepExecution, WorkflowRegistry
from domain.workflows.run import (
    RunStatus,
    StepOutcome,
    WorkflowRun,
    WorkflowRunRepository,
)
from domain.workforce import directions as carried
from domain.workforce.directions import Directions
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)


def order_steps(steps: tuple[WorkflowStep, ...]) -> tuple[WorkflowStep, ...]:
    """Declared order made runnable, or a clear complaint about why it is not.

    An unknown dependency and a cycle are both reported by name. A workflow is
    written by hand, so the error a person sees is part of the feature.
    """
    by_name = {step.name: step for step in steps}
    if len(by_name) != len(steps):
        counts = Counter(step.name for step in steps)
        duplicated = sorted(name for name, count in counts.items() if count > 1)
        raise ConfigurationError(f"Duplicate step name(s): {', '.join(duplicated)}")

    for step in steps:
        unknown = sorted(set(step.depends_on) - set(by_name))
        if unknown:
            raise ConfigurationError(
                f"Step '{step.name}' depends on {', '.join(unknown)}, which does not exist."
            )

    ordered: list[WorkflowStep] = []
    done: set[str] = set()
    remaining = list(steps)
    while remaining:
        ready = [step for step in remaining if set(step.depends_on) <= done]
        if not ready:
            stuck = ", ".join(sorted(step.name for step in remaining))
            raise ConfigurationError(f"Steps depend on each other in a cycle: {stuck}")
        # Declaration order among the ready ones, so a workflow with no
        # dependencies at all runs exactly as it reads.
        ordered.extend(ready)
        done.update(step.name for step in ready)
        remaining = [step for step in remaining if step.name not in done]
    return tuple(ordered)


class WorkflowEngine:
    """Implements running a `WorkflowDefinition`. Employees are not consulted."""

    def __init__(
        self,
        registry: WorkflowRegistry,
        execution: StepExecution,
        runs: WorkflowRunRepository,
    ) -> None:
        self._registry = registry
        self._execution = execution
        self._runs = runs

    async def run(
        self,
        name: str,
        *,
        version: int | None = None,
        inputs: dict[str, object] | None = None,
        trigger: WorkflowTrigger = WorkflowTrigger.MANUAL,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> WorkflowRun:
        definition = (
            self._registry.get(name)
            if version is None
            else self._registry.get(name, version)
        )
        return await self.run_definition(
            definition,
            inputs=inputs,
            trigger=trigger,
            workspace_id=workspace_id,
        )

    async def run_definition(
        self,
        definition: WorkflowDefinition,
        *,
        inputs: dict[str, object] | None = None,
        trigger: WorkflowTrigger = WorkflowTrigger.MANUAL,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> WorkflowRun:
        try:
            values = definition.values(inputs)
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        steps = order_steps(definition.steps)
        if definition.budget.max_steps is not None:
            attempts = sum(max(1, step.max_attempts) for step in steps)
            if attempts > definition.budget.max_steps:
                raise ConfigurationError(
                    f"Workflow '{definition.name}' can use {attempts} step attempts, "
                    f"above its budget of {definition.budget.max_steps}."
                )

        run = WorkflowRun.create(
            definition.name,
            workflow_version=definition.version,
            trigger=trigger,
            inputs=dict(values),
            workspace_id=workspace_id,
        )
        # Written before the first step, so a killed process leaves a run to
        # account for rather than a set of tasks nobody can attribute.
        await self._runs.save(run)
        log.info("workflow.started", workflow=definition.name, run_id=str(run.id), steps=len(steps))

        produced: dict[str, str] = {}
        for step in steps:
            wall_limit = definition.budget.max_wall_time_seconds
            if wall_limit is not None:
                elapsed = (datetime.now(UTC) - run.started_at).total_seconds()
                if elapsed >= wall_limit:
                    run = run.finished(
                        RunStatus.FAILED,
                        f"Workflow wall-time budget of {wall_limit:g}s was exhausted.",
                    )
                    await self._runs.save(run)
                    return run
            try:
                if wall_limit is None:
                    outcome = await self._run_step(
                        step, definition, values, produced, workspace_id
                    )
                else:
                    elapsed = (datetime.now(UTC) - run.started_at).total_seconds()
                    async with asyncio.timeout(max(0.001, wall_limit - elapsed)):
                        outcome = await self._run_step(
                            step, definition, values, produced, workspace_id
                        )
            except TimeoutError:
                run = run.finished(
                    RunStatus.FAILED,
                    f"Workflow wall-time budget of {wall_limit:g}s was exhausted.",
                )
                await self._runs.save(run)
                return run
            run = run.with_step(outcome)
            await self._runs.save(run)
            cost_limit = definition.budget.max_cost_usd
            if cost_limit is not None and run.cost_usd > cost_limit:
                run = run.finished(
                    RunStatus.FAILED,
                    f"Workflow cost budget of ${cost_limit:g} was exceeded.",
                )
                await self._runs.save(run)
                return run
            if outcome.succeeded:
                produced[step.name] = outcome.summary
                continue
            if step.on_failure is OnFailure.STOP:
                run = run.finished(RunStatus.FAILED, f"Step '{step.name}' failed.")
                await self._runs.save(run)
                log.warning("workflow.stopped", workflow=definition.name, step=step.name)
                return run
            log.info("workflow.step_skipped_failure", workflow=definition.name, step=step.name)

        failed = [step.step for step in run.steps if not step.succeeded]
        summary = (
            f"{len(run.steps)} step(s) ran; {', '.join(failed)} failed."
            if failed
            else f"{len(run.steps)} step(s) completed."
        )
        run = run.finished(RunStatus.COMPLETED, summary)
        await self._runs.save(run)
        log.info("workflow.finished", workflow=definition.name, run_id=str(run.id))
        return run

    def dry_run(
        self,
        name: str,
        *,
        version: int | None = None,
        inputs: dict[str, object] | None = None,
    ) -> dict[str, object]:
        definition = (
            self._registry.get(name)
            if version is None
            else self._registry.get(name, version)
        )
        try:
            values = definition.values(inputs)
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        steps = order_steps(definition.steps)
        return {
            "workflow": definition.name,
            "version": definition.version,
            "inputs": values,
            "steps": [
                {
                    "name": step.name,
                    "employee": step.employee,
                    "instruction": self._instruction(step, values, {}),
                    "depends_on": list(step.depends_on),
                    "max_attempts": step.max_attempts,
                }
                for step in steps
            ],
        }

    async def _run_step(
        self,
        step: WorkflowStep,
        definition: WorkflowDefinition,
        values: dict[str, object],
        produced: dict[str, str],
        workspace_id: WorkspaceId,
    ) -> StepOutcome:
        goal = self._instruction(step, values, produced)
        outcome = StepOutcome(step=step.name, employee=step.employee)
        for attempt in range(1, max(1, step.max_attempts) + 1):
            try:
                with carried.given(
                    Directions(
                        approvals=definition.profile.approvals,
                        model=definition.profile.model,
                    )
                ):
                    task = await self._execution.submit_and_run(
                        goal,
                        step.employee,
                        created_by=TaskCreatedBy.WORKFLOW,
                        workspace_id=workspace_id,
                    )
            except Exception as error:
                # A step that could not even start - an employee that is not
                # declared here, a provider with no key - is a failed step, not
                # a crashed run. The rest of the workflow still has a verdict.
                log.warning(
                    "workflow.step_failed",
                    workflow=definition.name,
                    step=step.name,
                    attempt=attempt,
                    error=str(error),
                )
                outcome = replace(outcome, attempts=attempt, summary=str(error))
                continue
            outcome = replace(
                outcome,
                task_id=task.id,
                attempts=attempt,
                succeeded=task.status is TaskStatus.COMPLETED,
                summary=self._summary_of(task),
                cost_usd=task.cost_usd,
            )
            if outcome.succeeded:
                return outcome
        return outcome

    @staticmethod
    def _summary_of(task: Task) -> str:
        return task.result.summary if task.result is not None else ""

    @staticmethod
    def _instruction(
        step: WorkflowStep, values: dict[str, object], produced: dict[str, str]
    ) -> str:
        """The step's text with the run's inputs and earlier results filled in.

        A missing placeholder is left as it was written rather than raising. The
        instruction is prose handed to a model, and half an instruction is worse
        than an instruction with a literal brace in it - the model can say it
        does not understand, while a crash mid-run cannot.
        """
        substitutions = {**values, **{f"steps.{k}": v for k, v in produced.items()}}
        text = step.instruction
        for key, value in substitutions.items():
            text = text.replace("{" + str(key) + "}", str(value))
        return text
