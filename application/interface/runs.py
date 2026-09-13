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
from uuid import UUID

import structlog

from application.prometheus.manager import PrometheusManager
from application.task_runner import TaskRunner
from domain.approvals.protocols import ApprovalWaiter
from domain.tasks.cancellation import Cancellations
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workforce import directions as carried
from domain.workforce.directions import NONE, Directions
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from domain.workforce.repository import ObjectiveRepository
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How long a shutdown waits for running tasks to stop themselves before they
#: are interrupted. Long enough for a step to finish, short enough that Ctrl-C
#: still feels like Ctrl-C.
SHUTDOWN_GRACE_SECONDS = 10.0


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
    ) -> None:
        self._runner = runner
        self._manager = manager
        self._tasks = tasks
        self._objectives_store = objectives
        self._cancellations = cancellations
        self._approvals = approvals
        self._running: dict[UUID, asyncio.Task[Task]] = {}
        self._objectives: dict[UUID, asyncio.Task[ObjectiveResult]] = {}

    # --- Starting -------------------------------------------------------------

    async def start(self, goal: str, employee_name: str) -> Task:
        """Record the task, schedule the work, and hand the task straight back."""
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
        objective = await self._manager.receive(
            request, workspace_id=workspace_id, conversation_id=conversation_id
        )
        work = asyncio.create_task(
            self._carry(objective, directions), name=f"prometheus-objective-{objective.id}"
        )
        self._objectives[objective.id] = work
        work.add_done_callback(lambda _: self._objectives.pop(objective.id, None))
        return objective

    async def _carry(self, objective: Objective, directions: Directions) -> ObjectiveResult:
        # Set inside the coroutine rather than around `create_task`, so the
        # directions belong to this objective's context and to every task it
        # starts - and never to the request handler that happened to submit it.
        with carried.given(directions):
            return await self._manager.handle_objective(objective)

    def is_thinking(self, objective_id: UUID) -> bool:
        return objective_id in self._objectives

    def _finished(self, task_id: UUID) -> None:
        self._running.pop(task_id, None)
        # A cancellation that outlived its run would silently stop the next one
        # started under the same id - which `resume` is entitled to do.
        self._cancellations.clear(task_id)

    def is_running(self, task_id: UUID) -> bool:
        return task_id in self._running

    @property
    def running(self) -> frozenset[UUID]:
        return frozenset(self._running)

    # --- Stopping -------------------------------------------------------------

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
        for task_id in list(self._running):
            self._cancellations.cancel(task_id, "The objective was stopped.")
            self._approvals.release(task_id)
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
        """Stop carrying anything, without leaving a run half-written.

        Every live run is asked to stop the cooperative way first, so it writes
        its own terminal state; only a run that ignores that is cancelled
        outright, and a run interrupted that way is resumable by construction.
        """
        # Remembered before cancelling: a finished coroutine takes itself out of
        # the map. An objective is not resumable the way a task is, so one this
        # process was carrying is closed rather than left looking busy in every
        # window opened after the next start.
        carried_objectives = list(self._objectives)
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
        for objective_id in carried_objectives:
            try:
                await self._close(objective_id)
            except Exception as error:  # a store already closing must not stop the shutdown
                log.warning(
                    "objective.not_closed", objective_id=str(objective_id), error=str(error)
                )

    # --- Approvals ------------------------------------------------------------

    def decide(self, approval_id: UUID, approved: bool) -> bool:
        """Answer a question a tool call is parked on.

        False means no call in this process was waiting on it - a row left by an
        earlier run, which the caller records against instead.
        """
        return self._approvals.decide(approval_id, approved)
