"""The tasks this process is carrying, and how to stop one.

The CLI runs a task and waits for it; an interface cannot. A request that starts
a task has to return the moment the task exists, because the point of the page
is to watch the run - so the work goes onto the event loop and the request
returns an id.

That leaves this file owning the two things a background run needs and a
foreground one does not.

**A task must exist before its request returns.** `submit` writes the task and
its assignment, and only then is the run scheduled. A page that got an id back
for a task the database has never heard of would be lying, and a process killed
one second later would have nothing to resume.

**An objective is carried the same way, one level up.** Since Phase 7 the page
asks Prometheus for an outcome rather than handing a task to an employee, and Prometheus's own
work - reading the request, planning, delegating, checking - takes as long as
the tasks it starts. So it too goes on the loop and the request returns an
objective id, which is what the page then watches.

**The approvals it holds are a contract, not an adapter.** It needs to release
a cancelled run's parked questions, and it takes `ApprovalWaiter` to do it -
which is what lets this live in the application layer rather than inside one
interface, where it sat until Phase 13 and where a second interface could not
reach it.

**Cancelling has two cases, and they are not the same.** A task this process is
running is asked to stop and stops itself between steps, keeping what it did. A
task that is *not* running - left behind by a killed process, or never started -
has nobody to ask, so it is moved to CANCELLED here. Conflating the two would
either leave a stale task uncancellable, or "cancel" a live run by writing a
terminal status underneath a loop that is still working.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import structlog

from application.prometheus.manager import PrometheusManager
from application.task_runner import TaskRunner
from domain.approvals.protocols import ApprovalWaiter
from domain.errors import InvalidStateTransitionError, WorkControlError, WorkStoppedError
from domain.policies.models import ActorKind
from domain.safety.emergency import EmergencyStop
from domain.tasks.cancellation import Cancellations, LiveTasks
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workforce import directions as carried
from domain.workforce.assignment import SharedContext
from domain.workforce.directions import NONE, Directions
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from domain.workforce.repository import ObjectiveRepository
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How long a shutdown waits for running tasks to stop themselves before they
#: are interrupted. Long enough for a step to finish, short enough that Ctrl-C
#: still feels like Ctrl-C.
SHUTDOWN_GRACE_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class HaltedWork:
    """What one sweep of the emergency stop reached in this process and the store."""

    objectives_cancelled: int = 0
    tasks_signalled: int = 0
    tasks_closed: int = 0
    approvals_released: int = 0


class Runs:
    """Starts tasks in the background and keeps track of the live ones."""

    def __init__(
        self,
        *,
        runner: TaskRunner,
        manager: PrometheusManager,
        tasks: TaskRepository,
        objectives: ObjectiveRepository,
        cancellations: Cancellations,
        approvals: ApprovalWaiter,
        stop: EmergencyStop | None = None,
    ) -> None:
        self._runner = runner
        self._manager = manager
        self._tasks = tasks
        self._objectives_store = objectives
        self._cancellations = cancellations
        self._approvals = approvals
        self._stop = stop
        self._running: dict[UUID, asyncio.Task[Task]] = {}
        self._objectives: dict[UUID, asyncio.Task[ObjectiveResult | None]] = {}

    # --- Starting -------------------------------------------------------------

    async def start(self, goal: str, employee_name: str) -> Task:
        """Record the task, schedule the work, and hand the task straight back."""
        self._refuse_if_stopped()
        task, assignment = await self._runner.submit(goal, employee_name)
        run = asyncio.create_task(
            self._runner.run(task, assignment), name=f"prometheus-task-{task.id}"
        )
        self._running[task.id] = run
        run.add_done_callback(lambda _: self._finished(task.id))
        return task

    async def ask(
        self,
        request: str,
        *,
        conversation_id: UUID | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        directions: Directions = NONE,
    ) -> Objective:
        """Record the objective, schedule Prometheus, and hand the objective back.

        The workspace is passed through rather than defaulted here. It was
        declared on `UserRequest` in Phase 13 and dropped on this line, which
        was invisible while there was one workspace and would have made
        switching between two look like it worked (§15.2).
        """
        self._refuse_if_stopped()
        # Received under the directions too, so the record says what they were.
        with carried.given(directions):
            objective = await self._manager.receive(
                request, workspace_id=workspace_id, conversation_id=conversation_id
            )
        self._schedule(objective, directions, resume=False)
        return objective

    async def reconcile_abandoned_approvals(self, workspaces: list[WorkspaceId]) -> int:
        """Tasks a crash left parked on a question nobody can answer any more."""
        if self._runner is None:
            return 0
        return sum(
            [await self._runner.reconcile_abandoned_approvals(item) for item in workspaces]
        )

    async def recover(self) -> int:
        """Resume every durable objective left incomplete by an earlier process.

        Not while stopped: the restart that follows a stop must not quietly pick
        up the work the stop was pulled on. The stop control closes it instead.
        """
        if self._stop is not None and self._stop.engaged():
            log.info("objectives.recovery_withheld", reason=self._stop.reason)
            return 0
        recovered = 0
        for objective in await self._objectives_store.list_incomplete():
            if objective.status is ObjectiveStatus.PAUSED:
                continue
            if objective.id in self._objectives:
                continue
            self._schedule(objective, objective.directions, resume=True)
            recovered += 1
        if recovered:
            log.info("objectives.recovered", count=recovered)
        return recovered

    def _schedule(self, objective: Objective, directions: Directions, *, resume: bool) -> None:
        work = asyncio.create_task(
            self._carry(objective, directions, resume=resume),
            name=f"prometheus-objective-{objective.id}",
        )
        self._objectives[objective.id] = work
        work.add_done_callback(lambda _: self._objectives.pop(objective.id, None))

    async def _carry(
        self, objective: Objective, directions: Directions, *, resume: bool = False
    ) -> ObjectiveResult | None:
        # Set inside the coroutine rather than around `create_task`, so the
        # directions belong to this objective's context and to every task it
        # starts - and never to the request handler that happened to submit it.
        with carried.given(directions):
            try:
                if resume:
                    return await self._manager.resume_objective(objective)
                return await self._manager.handle_objective(objective)
            except Exception as error:
                # Nobody awaits this coroutine, so an error raised out of it is
                # only ever a warning at garbage collection. The manager has
                # already closed the record and told the watchers; this is the
                # line in the log.
                log.warning(
                    "objective.failed",
                    objective_id=str(objective.id),
                    error=f"{type(error).__name__}: {error}",
                )
                return None

    def is_thinking(self, objective_id: UUID) -> bool:
        return objective_id in self._objectives

    async def wait_objective(self, objective_id: UUID) -> ObjectiveResult | None:
        """Wait for a background objective when this process is carrying it."""
        work = self._objectives.get(objective_id)
        if work is not None:
            return await work
        stored = await self._objectives_store.get(objective_id)
        return stored.result if stored is not None else None

    def _finished(self, task_id: UUID) -> None:
        self._running.pop(task_id, None)
        # A cancellation that outlived its run would silently stop the next one
        # started under the same id - which `resume` is entitled to do.
        self._cancellations.clear(task_id)

    def is_running(self, task_id: UUID) -> bool:
        return task_id in self._running

    def is_objective_running(self, objective_id: UUID) -> bool:
        return objective_id in self._objectives

    @property
    def running(self) -> frozenset[UUID]:
        return frozenset(self._running)

    @property
    def carrying(self) -> int:
        """How many runs this process would stop if it stopped now."""
        return len(self._running) + len(self._objectives)

    # --- Stopping -------------------------------------------------------------

    def _refuse_if_stopped(self) -> None:
        if self._stop is not None and self._stop.engaged():
            raise WorkStoppedError(self._stop.reason)

    async def halt_all(
        self,
        reason: str,
        unfinished: list[Task] | None = None,
        *,
        live: LiveTasks | None = None,
    ) -> HaltedWork:
        """Stop everything this process carries and close what nobody carries.

        The order is the point. Parked approvals are answered no first, so a
        call waiting on a person cannot be approved into the gap; every
        unfinished task is asked to stop at its next boundary, which keeps what
        it did; the manager's coroutines are cancelled and their records closed;
        and a task no runtime here is carrying - left by a crash - is closed in
        the store, so a later `resume` does not pick it up.
        """
        released = 0
        for request in self._approvals.pending():
            released += self._approvals.release(request.task_id)
        signalled = 0
        known = {task.id: task for task in unfinished or []}
        for task_id in {*self._running, *known}:
            self._cancellations.cancel(task_id, reason)
            released += self._approvals.release(task_id)
            signalled += 1
        cancelled = 0
        for objective_id in list(self._objectives):
            await self.cancel_objective(objective_id)
            cancelled += 1
        for objective in await self._objectives_store.list_incomplete():
            await self._close(objective.id)
            cancelled += 1
        closed = 0
        for task in known.values():
            carried_here = task.id in self._running or (
                live is not None and live.carrying(task.id)
            )
            if carried_here:
                continue
            current = await self._tasks.get(task.id)
            if current is None or current.is_terminal:
                continue
            try:
                stopped, event = current.transition_to(
                    TaskStatus.CANCELLED,
                    result=current.result or TaskResult(summary=f"Stopped: {reason}"),
                )
            except InvalidStateTransitionError:
                continue
            await self._tasks.save(stopped, event)
            # Nothing is carrying it, so nothing will clear the request either.
            self._cancellations.clear(task.id)
            closed += 1
        return HaltedWork(
            objectives_cancelled=cancelled,
            tasks_signalled=signalled,
            tasks_closed=closed,
            approvals_released=released,
        )

    async def pause(self, task_id: UUID) -> Task | None:
        """Ask a task to park at its next safe execution boundary."""
        task = await self._tasks.get(task_id)
        if task is None or task.is_terminal:
            return task
        self._cancellations.pause(task_id)
        log.info("task.pause_signalled", task_id=str(task_id))
        return task

    async def resume(self, task_id: UUID) -> Task | None:
        """Release a cooperative pause without restarting completed work."""
        task = await self._tasks.get(task_id)
        if task is None or task.is_terminal:
            return task
        self._refuse_if_stopped()
        self._cancellations.resume(task_id)
        return task

    async def handoff(self, task: Task, employee: str) -> Task:
        """Continue stopped work as a new child task with explicit ownership.

        A running task must be paused first. Replacing its employee in place
        would let two runtimes believe they own one cursor and one call ledger.
        """
        if task.status not in {
            TaskStatus.PAUSED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            raise WorkControlError(
                "Pause the task before handing it off, or hand off a failed task."
            )
        if task.status is TaskStatus.PAUSED and task.plan_id is not None:
            raise WorkControlError(
                "A planned task cannot change owners while its objective is active. "
                "Cancel the objective first, then retry the task with another employee."
            )
        if task.status is TaskStatus.PAUSED:
            self._cancellations.cancel(task.id, f"Handed off to {employee}.")
            self._approvals.release(task.id)
        context = SharedContext(
            facts=(f"Continue task {task.id}; preserve its completed work.",),
            artifacts=task.result.artifacts if task.result else (),
        )
        child, assignment = await self._runner.submit(
            task.goal,
            employee,
            created_by=task.created_by,
            assigned_by=ActorKind.USER,
            context=context,
            workspace_id=task.workspace_id,
            parent_id=task.id,
            plan_id=task.plan_id,
            assignment_reason=f"handed off by the user from task {task.id}",
        )
        run = asyncio.create_task(
            self._runner.run(child, assignment),
            name=f"prometheus-handoff-{child.id}",
        )
        self._running[child.id] = run
        run.add_done_callback(lambda _: self._finished(child.id))
        return child

    def resume_objective(self, objective: Objective) -> None:
        """Continue a paused durable objective when no live coroutine owns it."""
        self._refuse_if_stopped()
        if objective.id not in self._objectives:
            self._schedule(objective, objective.directions, resume=True)

    async def cancel(self, task_id: UUID, reason: str = "") -> Task | None:
        """Ask a task to stop. Returns the task, or None if there is no such task."""
        task = await self._tasks.get(task_id)
        if task is None:
            return None
        if task.is_terminal:
            return task

        if self.is_running(task_id):
            self._cancellations.cancel(task_id, reason)
            # A run parked on an approval is not between steps and would not see
            # the request until the question times out. Releasing it answers no,
            # which is the same answer the timeout would eventually give.
            released = self._approvals.release(task_id)
            log.info(
                "task.cancel_signalled",
                task_id=str(task_id),
                approvals_released=released,
            )
            return task

        cancelled, event = task.transition_to(
            TaskStatus.CANCELLED,
            result=task.result or TaskResult(summary=reason or "Cancelled before it ran."),
        )
        await self._tasks.save(cancelled, event)
        log.info("task.cancelled_while_idle", task_id=str(task_id), status=task.status.value)
        return cancelled

    async def signal_cancel(self, task_id: UUID, reason: str = "") -> Task | None:
        """Ask known objective work to stop without guessing that it is idle.

        Manager-owned tasks are awaited inside the objective coroutine rather
        than registered in ``_running``. Their employee run is shielded from
        the manager cancellation and observes this signal at a safe boundary.
        """
        task = await self._tasks.get(task_id)
        if task is None or task.is_terminal:
            return task
        self._cancellations.cancel(task_id, reason)
        self._approvals.release(task_id)
        return task

    async def cancel_objective(self, objective_id: UUID) -> bool:
        """Stop Prometheus working on one objective, and every task it has running.

        The objective's own coroutine is cancelled outright - it holds no
        outside state, only the decision about what to do next - while its tasks
        are asked to stop the cooperative way, which is what keeps whatever they
        had already done.

        Then the record is closed, whether or not anything here was carrying it.
        Cancelling the coroutine writes nothing, and an objective left behind by
        a process that is gone has no coroutine to cancel; either way the row
        would stay at the stage it had reached, and every interface would go on
        showing it as work in progress for good.
        """
        work = self._objectives.get(objective_id)
        if work is not None:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await self._close(objective_id)
        return work is not None

    async def _close(self, objective_id: UUID) -> None:
        objective = await self._objectives_store.get(objective_id)
        if objective is None or objective.is_terminal:
            return
        stopped = objective.to(
            ObjectiveStatus.CANCELLED,
            ObjectiveResult(
                objective_id=objective_id,
                summary="Stopped before it was finished.",
                status=ObjectiveStatus.CANCELLED,
            ),
        )
        await self._objectives_store.save(stopped)
        log.info("objective.cancelled", objective_id=str(objective_id))

    async def aclose(self) -> None:
        """Stop carrying work while leaving durable objectives recoverable.

        Objective coroutines are interrupted without closing their records.
        The next process reads those records and resumes their persisted plans.
        Direct employee runs retain their cooperative shutdown behaviour.
        """
        for work in self._objectives.values():
            work.cancel()
        for task_id in list(self._running):
            self._cancellations.cancel(task_id, "The interface is shutting down.")
            self._approvals.release(task_id)
        pending = [*self._running.values(), *self._objectives.values()]
        if pending:
            done, still_running = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
            del done
            for run in still_running:
                run.cancel()
            await asyncio.gather(*still_running, return_exceptions=True)

    # --- Approvals ------------------------------------------------------------

    def decide(self, approval_id: UUID, approved: bool) -> bool:
        """Answer a question a tool call is parked on.

        False means no call in this process was waiting on it - a row left by an
        earlier run, which the caller records against instead.
        """
        return self._approvals.decide(approval_id, approved)
