"""Starts tasks, resumes them, and retries the failures worth retrying.

This is the entry point every interface uses - the CLI today, the local UI in
Phase 6, Prometheus in Phase 7 - so that "run a task" means the same thing regardless
of who asked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

import structlog

from application.employee_runtime.runtime import EmployeeRuntime
from application.orchestrator import Failure, classify
from domain.employees.definition import EmployeeDefinition
from domain.employees.protocols import EmployeeRegistry
from domain.llm import escalation, routing
from domain.policies.models import ActorKind
from domain.tasks.progress import NullProgress, ProgressEvent, ProgressKind, ProgressSink
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task, TaskCreatedBy, TaskError, TaskResult, TaskStatus
from domain.workforce.acceptance import accept
from domain.workforce.assignment import AssignmentOutcome, SharedContext, TaskAssignment
from domain.workforce.decision import Decision, SelectionCode
from domain.workforce.repository import AssignmentRepository
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from domain.workspace.protocols import WorkspaceContext

log = structlog.get_logger(__name__)

#: How many times a transient failure is worth re-entering the task for. The
#: provider adapter already retries individual calls; this covers a failure that
#: killed the whole run.
MAX_TASK_ATTEMPTS = 2


class TaskRunner:
    def __init__(
        self,
        *,
        tasks: TaskRepository,
        assignments: AssignmentRepository,
        registry: EmployeeRegistry,
        build_runtime: Callable[[EmployeeDefinition], Awaitable[EmployeeRuntime]],
        max_attempts: int = MAX_TASK_ATTEMPTS,
        progress: ProgressSink | None = None,
        workspaces: WorkspaceContext | None = None,
    ) -> None:
        self._tasks = tasks
        self._assignments = assignments
        self._registry = registry
        self._build_runtime = build_runtime
        self._max_attempts = max_attempts
        # The task carries the workspace it was created in, and the run has to
        # keep it: a person switching workspace mid-run must not move the root
        # the run's file tools resolve against, and two tasks running at once in
        # different workspaces must not resolve one relative path to one file.
        # None means nothing switches, which is every configuration before
        # Phase 15 and every test that does not care.
        self._workspaces = workspaces
        # A failure classified here never reaches the runtime's announcer: the
        # run it would have announced from is the one that just died.
        self._progress = progress or NullProgress()

    # --- Starting -------------------------------------------------------------

    async def submit(
        self,
        goal: str,
        employee_name: str,
        *,
        created_by: TaskCreatedBy = TaskCreatedBy.USER,
        assigned_by: ActorKind = ActorKind.USER,
        context: SharedContext | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        parent_id: UUID | None = None,
        plan_id: UUID | None = None,
        assignment_reason: str = "chosen directly by the user",
    ) -> tuple[Task, TaskAssignment]:
        """Record the task and who it went to, before any work starts.

        Written down first so that a process killed one second later still has a
        task to come back to.
        """
        definition = self._registry.get(employee_name)
        task = replace(
            Task.create(
                goal,
                workspace_id=workspace_id,
                created_by=created_by,
                parent_id=parent_id,
            ),
            assigned_employee_id=definition.id,
            plan_id=plan_id,
            assignment_reason=assignment_reason,
        )
        await self._tasks.save(task)

        assignment = TaskAssignment.create(
            task_id=task.id,
            employee_id=definition.id,
            assigned_by=assigned_by,
            context=context or SharedContext(),
            workspace_id=workspace_id,
            # Nobody chose among candidates here: the employee was named, by a
            # person or by a workflow step, and the record says which.
            decision=Decision(
                code=(
                    SelectionCode.DECLARED_BY_WORKFLOW
                    if created_by is TaskCreatedBy.WORKFLOW
                    else SelectionCode.CHOSEN_BY_PERSON
                ),
                reason=assignment_reason,
            ),
        ).accept()
        await self._assignments.save(assignment)
        log.info(
            "task.submitted",
            task_id=str(task.id),
            employee=definition.name,
            assigned_by=assigned_by.value,
        )
        await self._announce(
            task,
            ProgressKind.STAGE,
            f"Assigned to {definition.name}.",
            payload={"employee": definition.name, "goal": task.goal},
        )
        return task, assignment

    async def run(self, task: Task, assignment: TaskAssignment | None = None) -> Task:
        """Carry one task to a terminal state, retrying transient failures."""
        if self._workspaces is None:
            return await self._run(task, assignment)
        with self._workspaces.enter(task.workspace_id):
            return await self._run(task, assignment)

    async def _run(self, task: Task, assignment: TaskAssignment | None = None) -> Task:
        attempt = 1

        while True:
            try:
                # Assembling the runtime is inside the try on purpose: a missing
                # employee or an unconfigured provider fails here, and that is a
                # failure of the task like any other, not an escaping exception.
                definition = self._definition_for(task)
                runtime = await self._build_runtime(definition)
                # Every model call this run makes is billed to this task, and
                # routed a step up if the manager sent it back as an escalation.
                # Read off the assignment, so a resumed escalation stays one.
                carried = escalation.Escalation.from_dict(
                    assignment.context.data.get("escalation") if assignment else None
                )
                with routing.billing(task.id), escalation.given(carried):
                    await runtime.run(task, assignment)
                break
            except Exception as error:
                failure = classify(error)
                log.warning(
                    "task.failed",
                    task_id=str(task.id),
                    kind=failure.kind.value,
                    error_type=failure.error_type,
                    attempt=attempt,
                )
                if failure.is_retryable and attempt < self._max_attempts:
                    attempt += 1
                    task = await self._reload(task)
                    await asyncio.sleep(0)
                    continue
                task = await self._mark_failed(task, failure)
                break

        final = await self._reload(task)
        if assignment is not None:
            await self._close(assignment, final)
        return final

    async def start(self, task: Task, assignment: TaskAssignment) -> Task:
        """Run a task somebody else composed. Implements `TaskExecution`.

        The manager creates its own tasks - a plan's dependency edges point at
        ids that have to exist before anything runs - and picks the employee
        itself. So this is `submit` with those two decisions already made:
        persist both, then carry the task to a terminal state.
        """
        await self._tasks.save(task)
        await self._assignments.save(assignment.accept())
        log.info(
            "task.started",
            task_id=str(task.id),
            employee_id=str(assignment.employee_id),
            assigned_by=assignment.assigned_by.value,
        )
        await self._announce(
            task,
            ProgressKind.STAGE,
            f"Starting: {task.goal}",
            payload={"assigned_by": assignment.assigned_by.value},
        )
        # Cancelling the manager must not interrupt an employee in the middle
        # of an external action. The interface signals cooperative cancellation
        # first; the shielded run reaches its next safe boundary and records
        # what actually happened even after the objective coroutine closes.
        return await asyncio.shield(self.run(task, assignment))

    async def submit_and_run(
        self, goal: str, employee_name: str, **kwargs: object
    ) -> Task:
        task, assignment = await self.submit(goal, employee_name, **kwargs)  # type: ignore[arg-type]
        return await self.run(task, assignment)

    # --- Resuming -------------------------------------------------------------

    async def resumable(self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID) -> list[Task]:
        return await self._tasks.list_resumable(workspace_id)

    async def history(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Task]:
        """What has been run here lately, newest first."""
        return await self._tasks.list_recent(workspace_id, limit=limit)

    async def resume(self, task: Task) -> Task:
        """Pick a task up where it stopped.

        The runtime reads its own stage and transcript out of `task.execution`,
        so resuming is the same call as starting - the difference is entirely in
        the state that was saved.
        """
        assignments = await self._assignments.for_task(task.id)
        log.info(
            "task.resuming",
            task_id=str(task.id),
            status=task.status.value,
            step=task.execution.step,
        )
        assignment = assignments[0] if assignments else None
        if assignment is None and task.assigned_employee_id is not None:
            # The process stopped between writing the task and writing who it
            # went to. One assignment is recorded now, saying so, rather than
            # none - a run nobody can attribute is missing from every count.
            assignment = TaskAssignment.create(
                task_id=task.id,
                employee_id=task.assigned_employee_id,
                assigned_by=ActorKind.PROMETHEUS
                if task.created_by is TaskCreatedBy.PROMETHEUS
                else ActorKind.USER,
                workspace_id=task.workspace_id,
                decision=Decision(
                    code=SelectionCode.UNRECORDED,
                    reason="recovered after a restart; the original decision was not saved",
                ),
            ).accept()
            await self._assignments.save(assignment)
        return await asyncio.shield(self.run(task, assignment))

    async def resume_all(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Task]:
        return [await self.resume(task) for task in await self.resumable(workspace_id)]

    # --- Internals ------------------------------------------------------------

    def _definition_for(self, task: Task) -> EmployeeDefinition:
        for definition in self._registry.list(task.workspace_id):
            if definition.id == task.assigned_employee_id:
                return definition
        from domain.errors import EmployeeNotFoundError

        raise EmployeeNotFoundError(str(task.assigned_employee_id))

    async def _reload(self, task: Task) -> Task:
        return await self._tasks.get(task.id) or task

    async def _mark_failed(self, task: Task, failure: Failure) -> Task:
        current = await self._reload(task)
        if current.is_terminal:
            return current
        failed, event = current.transition_to(
            TaskStatus.FAILED,
            error=TaskError(
                kind=failure.error_type, message=failure.message, details={"kind": failure.kind}
            ),
            attempts=current.attempts + 1,
        )
        await self._tasks.save(failed, event)
        await self._announce(
            failed,
            ProgressKind.RESULT,
            f"{failure.error_type}: {failure.message}",
            payload={"status": failed.status.value, "kind": failure.kind.value},
        )
        return failed

    async def _announce(
        self,
        task: Task,
        kind: ProgressKind,
        message: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        try:
            await self._progress.emit(
                ProgressEvent(
                    task_id=task.id,
                    kind=kind,
                    message=message,
                    step=task.execution.step,
                    payload=dict(payload or {}),
                    workspace_id=task.workspace_id,
                )
            )
        except Exception as error:  # a watcher must not be able to fail a run
            log.warning("progress.emit_failed", kind=kind.value, error=str(error))

    async def _close(self, assignment: TaskAssignment, task: Task) -> None:
        """Close the assignment with how the run ended and what the evidence supports.

        The verdict is the manager's rule (`domain.workforce.acceptance`) applied
        with the role's contract, recorded here because every way work is run
        passes through this method - a plan, a workflow step, a person naming
        an employee - and a statistic that counted only one of them would be a
        statistic about how work was started. It is a pure function of the
        record, so the manager reading the same task reaches the same verdict.
        A task that did not complete gets none: there was no result to judge.
        """
        outcome = {
            TaskStatus.COMPLETED: AssignmentOutcome.COMPLETED,
            # A person stopped it. Recorded as FAILED until Phase 11, which
            # charged every change of mind to the employee's record.
            TaskStatus.CANCELLED: AssignmentOutcome.CANCELLED,
        }.get(task.status, AssignmentOutcome.FAILED)
        closed = assignment.close(outcome, task.result or TaskResult(summary=""))
        if task.status is TaskStatus.COMPLETED:
            contract = next(
                (
                    definition.contract
                    for definition in self._registry.list(task.workspace_id)
                    if definition.id == assignment.employee_id
                ),
                None,
            )
            closed = closed.judged(accept(task, contract))
        await self._assignments.save(closed)
