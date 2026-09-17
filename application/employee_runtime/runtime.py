"""One runtime for every employee.

An employee is its declaration plus this loop. There is no researcher class and
there will not be an analyst class: a second runtime would mean the difference
between two employees had stopped being declarative, which is the thing this
design exists to prevent.

The stages are: plan, execute, verify - and verify can send the work back once.
State is written to the task after every stage and after every step inside
execution, which is what lets a killed process pick the task up again.

Memory joins the loop at the two moments it is worth anything: what was learned
before is recalled into the context planning starts from, and what happened is
written down once the task reaches a terminal state. Both are optional
dependencies - a runtime with no memory is the Phase 8 runtime, and a memory
that cannot be reached slows a run down rather than failing it.

One thing the runtime does with an assignment is worth naming: a delegator may
*narrow* what the employee may use for this task, and the runtime applies that
by intersecting it with the declaration. Narrowing works; widening cannot, since
the intersection can only ever shrink. Delegation therefore never escalates
privileges, and that is a property of this code rather than of the manager's
good behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import structlog

from application.employee_runtime.executor import Executor, StepOutcome
from application.employee_runtime.planner import Planner
from application.employee_runtime.transcript import RunState, Transcript
from application.employee_runtime.verifier import Verifier
from application.memory.assembler import AssembledContext, ContextAssembler
from application.memory.recorder import MemoryRecorder
from domain.employees.definition import EmployeeDefinition
from domain.employees.limits import ExecutionLimits, LimitKind
from domain.errors import PlanningError
from domain.tasks.plan import TaskPlan
from domain.tasks.progress import NullProgress, ProgressEvent, ProgressKind, ProgressSink
from domain.tasks.repository import TaskRepository
from domain.tasks.task import (
    Execution,
    Task,
    TaskResult,
    TaskStatus,
    can_transition,
)
from domain.tools.protocols import ToolRegistry
from domain.workforce.assignment import TaskAssignment

log = structlog.get_logger(__name__)

STAGE_PLANNING = "PLANNING"
STAGE_EXECUTING = "EXECUTING"
STAGE_VERIFYING = "VERIFYING"
STAGE_DONE = "DONE"

#: One retry after a rejected verdict. A second rejection usually means the task
#: is wrong or the evidence is not there, and a third attempt just spends more.
MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """What the runtime needs, all of it behind a domain contract."""

    planner: Planner
    executor: Executor
    verifier: Verifier
    tasks: TaskRepository
    tools: ToolRegistry
    limits: ExecutionLimits
    system_prompt: str = ""
    #: Where the run says what it is doing while it does it. Defaults to a sink
    #: that drops everything, which is what the CLI wants.
    progress: ProgressSink = field(default_factory=NullProgress)
    #: What earlier runs learned, assembled into this one's context. None means
    #: the employee starts every task knowing only what it was told.
    context: ContextAssembler | None = None
    #: Where this run's own outcome is written down. None means nothing is kept.
    recorder: MemoryRecorder | None = None


class EmployeeRuntime:
    """Implements `domain.employees.protocols.Employee`."""

    def __init__(self, definition: EmployeeDefinition, deps: RuntimeDependencies) -> None:
        self._definition = definition
        self._deps = deps

    @property
    def definition(self) -> EmployeeDefinition:
        return self._definition

    async def perform(self, assignment: TaskAssignment) -> TaskResult:
        task = await self._deps.tasks.get(assignment.task_id)
        if task is None:
            from domain.errors import TaskNotFoundError

            raise TaskNotFoundError(assignment.task_id)
        return await self.run(task, assignment)

    def _effective(self, assignment: TaskAssignment | None) -> EmployeeDefinition:
        """The declaration as it applies to this one assignment.

        `granted_tools` is what the delegator meant to allow. Intersecting is
        the whole mechanism: a manager can hand down less than the employee is
        trusted with and can never hand down more, whatever it puts in the
        assignment. An assignment that says nothing narrows nothing.
        """
        if assignment is None:
            return self._definition
        granted = assignment.context.data.get("granted_tools")
        if not isinstance(granted, list | tuple | set | frozenset):
            return self._definition
        narrowed = self._definition.allowed_tools & frozenset(str(name) for name in granted)
        if narrowed == self._definition.allowed_tools:
            return self._definition
        log.info(
            "assignment.narrowed",
            employee=self._definition.name,
            withheld=sorted(self._definition.allowed_tools - narrowed),
        )
        return replace(self._definition, allowed_tools=narrowed)

    async def run(self, task: Task, assignment: TaskAssignment | None = None) -> TaskResult:
        """Carry the task from wherever it is to a terminal state."""
        state = RunState.from_state(task.execution.state)
        definition = self._effective(assignment)
        logger = log.bind(task_id=str(task.id), employee=definition.name)

        while state.stage != STAGE_DONE:
            if state.stage == STAGE_PLANNING:
                task, state = await self._plan(task, state, assignment, definition)
            elif state.stage == STAGE_EXECUTING:
                task, state, result, outcome = await self._execute(task, state, definition)
                # A deliberate stop skips verification: nobody claimed the
                # work was finished, so there is nothing to check it against.
                if outcome.cancelled:
                    task, state = await self._cancel(task, state, result, definition)
                else:
                    task, state = await self._verify(
                        task, state, result, outcome.stopped_by, definition, logger
                    )
            else:  # a state written by an older version, or a corrupted resume
                logger.warning("task.unknown_stage", stage=state.stage)
                state = replace(state, stage=STAGE_PLANNING)

        final = task.result or TaskResult(summary="")
        return final

    # --- Stages ---------------------------------------------------------------

    async def _plan(
        self,
        task: Task,
        state: RunState,
        assignment: TaskAssignment | None,
        definition: EmployeeDefinition,
    ) -> tuple[Task, RunState]:
        if task.status is not TaskStatus.PLANNING:
            task, event = task.transition_to(TaskStatus.PLANNING)
            await self._deps.tasks.save(task, event)

        await self._announce(task, ProgressKind.STAGE, "Planning the work.")
        assembled = await self._context(task, definition, assignment)
        recalled = assembled.recollections()
        quoted = assembled.quotations()
        try:
            plan = await self._deps.planner.plan(
                task,
                definition,
                tools=self._deps.tools.list_specs(definition),
                context=assignment.context if assignment else None,
                recalled=recalled,
                knowledge=quoted,
            )
        except PlanningError as error:
            # A task with no plan is still worth attempting: the executor can
            # work from the goal alone, and that beats failing before starting.
            log.warning("task.planning_failed", task_id=str(task.id), error=str(error))
            plan = TaskPlan.of(task.goal, rationale="Planning failed; working from the goal.")

        transcript = Transcript(
            messages=Executor.opening_messages(
                task,
                definition,
                plan,
                self._deps.system_prompt,
                state.verifier_feedback,
                interfaces=tuple(
                    spec.interface_level for spec in self._deps.tools.list_specs(definition)
                ),
                recalled=recalled,
                knowledge=quoted,
            ),
            cost_usd=state.transcript.cost_usd,
        )
        state = replace(state, stage=STAGE_EXECUTING, transcript=transcript)
        task = replace(task, plan=plan)
        task, event = task.transition_to(TaskStatus.RUNNING)
        task = task.with_execution(Execution(step=transcript.steps, state=state.to_state()))
        await self._deps.tasks.save(task, event)
        await self._announce(
            task,
            ProgressKind.PLAN,
            plan.rationale or f"{len(plan.steps)} step(s) planned.",
            payload=plan.to_dict(),
        )
        return task, state

    async def _execute(
        self, task: Task, state: RunState, definition: EmployeeDefinition
    ) -> tuple[Task, RunState, TaskResult, StepOutcome]:
        current = task
        if current.status is TaskStatus.PAUSED:
            current, resumed = current.transition_to(TaskStatus.RUNNING)
            await self._deps.tasks.save(current, resumed)
        noted = 0

        async def persist(transcript: Transcript) -> None:
            # Saved before the next model call, so a process killed mid-run comes
            # back knowing what it already said and already paid for.
            nonlocal current, state, noted
            state = replace(state, transcript=transcript)
            current = current.with_execution(
                Execution(step=transcript.steps, state=state.to_state())
            )
            current = replace(current, cost_usd=transcript.cost_usd)
            await self._deps.tasks.save(current)
            # Working memory, written as the work happens rather than after it:
            # its whole audience is the second attempt at this same task, and a
            # note written at the end arrives after that attempt has started.
            if self._deps.recorder is not None:
                for observation in transcript.observations[noted:]:
                    await self._deps.recorder.note_step(current, definition, observation)
                noted = len(transcript.observations)

        async def waiting(status: TaskStatus) -> None:
            # The task really does stop running when a person is asked, and
            # `prometheus tasks` should be able to say so. Persisted rather than
            # kept in memory, because the question outlives the process that
            # asked it: a run killed while parked comes back parked.
            nonlocal current
            if current.status is status or not can_transition(current.status, status):
                return
            current, event = current.transition_to(status)
            await self._deps.tasks.save(current, event)

        outcome = await self._deps.executor.run(
            task, definition, state.transcript, on_step=persist, on_status=waiting
        )
        state = replace(state, transcript=outcome.transcript, stage=STAGE_VERIFYING)
        result = TaskResult(
            summary=outcome.answer,
            output={
                "steps": outcome.transcript.steps,
                "observations": [o.to_dict() for o in outcome.transcript.observations],
                "stopped_by": outcome.stopped_by.value if outcome.stopped_by else None,
            },
        )
        return current, state, result, outcome

    async def _verify(
        self,
        task: Task,
        state: RunState,
        result: TaskResult,
        stopped_by: LimitKind | None,
        definition: EmployeeDefinition,
        logger: structlog.BoundLogger,
    ) -> tuple[Task, RunState]:
        task, event = task.transition_to(TaskStatus.VERIFYING)
        await self._deps.tasks.save(task, event)
        await self._announce(task, ProgressKind.STAGE, "Checking the result against the goal.")

        verdict = await self._deps.verifier.verify(task, result, task.plan)

        if verdict.passed:
            task, event = task.transition_to(TaskStatus.COMPLETED, result=result)
            await self._deps.tasks.save(task, event)
            await self._announce(
                task,
                ProgressKind.RESULT,
                result.summary,
                payload={"status": task.status.value, "cost_usd": task.cost_usd},
            )
            await self._remember(task, definition)
            return task, replace(state, stage=STAGE_DONE)

        if state.attempt < MAX_ATTEMPTS and stopped_by is None:
            # Send it back once, told what was wrong. A retry that is not better
            # informed than the first attempt is just a repeat.
            logger.info("task.retrying", attempt=state.attempt + 1, reason=verdict.reason)
            task, event = task.transition_to(TaskStatus.PLANNING)
            await self._deps.tasks.save(task, event)
            state = replace(
                state,
                stage=STAGE_PLANNING,
                attempt=state.attempt + 1,
                verifier_feedback=verdict.missing or (verdict.reason,),
            )
            return task, state

        from domain.tasks.task import TaskError

        reason = (
            f"Stopped by the {stopped_by.value.lower()} limit; "
            f"the partial result did not pass verification: {verdict.reason}"
            if stopped_by is not None
            else verdict.reason
        )
        task, event = task.transition_to(
            TaskStatus.FAILED,
            error=TaskError(
                kind="VerificationFailed",
                message=reason,
                details={"missing": list(verdict.missing)},
            ),
            result=result,
            attempts=task.attempts + 1,
        )
        await self._deps.tasks.save(task, event)
        await self._announce(
            task,
            ProgressKind.RESULT,
            reason,
            payload={"status": task.status.value, "cost_usd": task.cost_usd},
        )
        await self._remember(task, definition)
        return task, replace(state, stage=STAGE_DONE)

    async def _cancel(
        self,
        task: Task,
        state: RunState,
        result: TaskResult,
        definition: EmployeeDefinition,
    ) -> tuple[Task, RunState]:
        """A person asked for this to stop.

        The partial result is kept rather than discarded, and the task is not
        verified: nobody claimed it was finished, so there is nothing to check
        it against, and a rejected verdict would misreport a deliberate stop as
        a failure.
        """
        task, event = task.transition_to(TaskStatus.CANCELLED, result=result)
        await self._deps.tasks.save(task, event)
        await self._announce(
            task,
            ProgressKind.RESULT,
            "Cancelled. What was done up to this point has been kept.",
            payload={"status": task.status.value, "cost_usd": task.cost_usd},
        )
        await self._remember(task, definition)
        return task, replace(state, stage=STAGE_DONE)

    # --- Memory ---------------------------------------------------------------

    async def _context(
        self,
        task: Task,
        definition: EmployeeDefinition,
        assignment: TaskAssignment | None,
    ) -> AssembledContext:
        """Goal, what was passed down, and what is remembered - in that order."""
        if self._deps.context is None:
            return AssembledContext(
                goal=task.goal,
                facts=assignment.context.facts if assignment else (),
                constraints=assignment.context.constraints if assignment else (),
            )
        return await self._deps.context.assemble(
            task, definition, assignment.context if assignment else None
        )

    async def _remember(self, task: Task, definition: EmployeeDefinition) -> None:
        """Write down what happened, once the task has finished happening.

        Called for a failure and a cancellation as well as a success: an
        approach that did not work is exactly what a later run needs to know,
        and it is the thing nobody writes down.
        """
        if self._deps.recorder is not None:
            await self._deps.recorder.record_task(task, definition)

    # --- Announcing -----------------------------------------------------------

    async def _announce(
        self,
        task: Task,
        kind: ProgressKind,
        message: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        try:
            await self._deps.progress.emit(
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
