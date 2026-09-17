"""Running a plan, and deciding what a failed task means.

The employee runtime already retries inside a task: a rejected verdict sends the
work back through planning once. This is the layer above, and it answers a
different question - not "should this task try again" but "does the objective
still have a route to done, and what is it".

Three answers, and they are not interchangeable (§7.7):

* **retry** - the same task, the same employee, because the failure was
  transient and the second attempt has as good a chance as the first;
* **reassign** - the same task, somebody else, because the employee could not
  reach what the task needed;
* **replan** - a different decomposition, because the task itself was wrong.

Retrying a task that nobody can do, or reassigning one that failed because the
provider was down, both burn a budget to arrive back where they started. So the
choice is made from the *kind* of failure, by type and by what the run reported,
never by the text of a message - the same rule `application/orchestrator.py`
follows one layer down.

Dependencies are honoured, and a task whose dependency failed is never started:
a summary written from a source that was never fetched is worse than no summary.

Phase 12 adds three things to that, and none of them changes the plan.

**Independent tasks run at once.** `Plan.ready` has always returned every task
whose dependencies are met; the difference is that they are now started
together, up to a limit, instead of one after another. Everything in one wave is
independent by construction - if two tasks needed ordering, the planner would
have written the edge - so this is the executor catching up with what the plan
already said. A limit rather than no limit: each task is a whole employee run
with its own model calls and its own tools, and a plan of six firing at once is
six browsers.

**A failure ends the plan, not the wave.** The tasks already in flight beside a
failing one are allowed to finish and their results are kept. They were never
downstream of it - that is what independent means - and throwing away work that
succeeded because something unrelated did not is a cost with nothing behind it.

**A result is accepted on the evidence.** `domain.workforce.acceptance` reads
what the task actually did off its own record. An employee that reported success
having had every tool call refused has not done the work, and the manager is the
one who has to say so: the employee's own verifier is the same run asked twice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum
from uuid import UUID

import structlog

from application.prometheus.delegation import CapabilityDelegator
from application.workforce.coordinator import WorkforceCoordinator
from domain.llm import escalation as escalations
from domain.llm.escalation import Escalation, EscalationAdvisor
from domain.llm.models import TaskKind
from domain.policies.models import ActorKind
from domain.tasks.progress import NullProgress, ProgressEvent, ProgressKind, ProgressSink
from domain.tasks.task import Task, TaskStatus
from domain.tools.refusals import REFUSED
from domain.workforce.acceptance import Acceptance, accept
from domain.workforce.assignment import SharedContext, TaskAssignment
from domain.workforce.protocols import Plan, PlanProgress, TaskExecution
from domain.workforce.routing import Requirement

log = structlog.get_logger(__name__)

#: How many times one task is put back to work before the plan is reconsidered.
#: Two, because the runtime has already retried once inside the task: a third
#: outer attempt is the fifth model conversation about the same instruction.
MAX_TASK_ATTEMPTS = 2

#: How many tasks of one plan run at the same time. Each is a whole employee
#: run - its own model calls, its own budget, possibly its own browser - so this
#: is a machine's limit, not a plan's, and a plan wide enough to exceed it runs
#: in two waves rather than failing.
MAX_PARALLEL_TASKS = 3


class Recovery(StrEnum):
    RETRY = "RETRY"
    REASSIGN = "REASSIGN"
    REPLAN = "REPLAN"
    GIVE_UP = "GIVE_UP"


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """One finished task, who did it, and whether the manager takes it."""

    task: Task
    employee: str
    reason: str = ""
    #: What the manager made of the evidence. A task can end COMPLETED and
    #: still not be a result: §88 is the whole reason this field is separate
    #: from `task.status`. The row keeps saying what the runtime concluded -
    #: rewriting it would lose the disagreement, which is the interesting part.
    acceptance: Acceptance = field(default_factory=Acceptance.taken)

    @property
    def succeeded(self) -> bool:
        return self.task.status is TaskStatus.COMPLETED and self.acceptance.accepted


@dataclass(frozen=True, slots=True)
class Supervision:
    """What became of a plan."""

    plan: Plan
    outcomes: tuple[TaskOutcome, ...]
    recovery: Recovery | None = None
    #: Why the plan stopped short, in the words the replanner will be given.
    shortfall: tuple[str, ...] = ()

    @property
    def progress(self) -> PlanProgress:
        completed = sum(1 for outcome in self.outcomes if outcome.succeeded)
        return PlanProgress(
            plan_id=self.plan.id,
            total=len(self.plan.tasks),
            completed=completed,
            failed=len(self.outcomes) - completed,
            running=0,
        )

    @property
    def all_succeeded(self) -> bool:
        return len(self.outcomes) == len(self.plan.tasks) and all(
            outcome.succeeded for outcome in self.outcomes
        )

    @property
    def cost_usd(self) -> float:
        return sum(outcome.task.cost_usd for outcome in self.outcomes)


def classify(task: Task) -> Recovery:
    """What a finished-but-not-completed task calls for next.

    Read from the run, not from prose: whether a budget stopped it, whether a
    tool was refused, whether verification rejected it. A task that was stopped
    by a person is not a failure to recover from at all.
    """
    if task.status is TaskStatus.COMPLETED:
        return Recovery.RETRY  # never consulted; kept total for the caller's sake
    if task.status is TaskStatus.CANCELLED:
        return Recovery.GIVE_UP
    if task.status is TaskStatus.WAITING_FOR_APPROVAL:
        # The Future that held this question belonged to the previous process.
        # Startup expires the row; only a person may decide what should happen
        # next, so neither retrying nor silently replanning is safe.
        return Recovery.GIVE_UP

    stopped_by = (task.result.output.get("stopped_by") if task.result else None) or None
    if stopped_by:
        # It ran out of budget rather than out of ideas. More of the same, from
        # the same employee, would run out again at the same place.
        return Recovery.REPLAN

    kind = task.error.kind if task.error else ""
    if kind in _TRANSIENT_ERRORS:
        return Recovery.RETRY
    if kind == "PermissionDeniedError" or _was_refused_a_tool(task):
        # The employee could not reach what the task needed. Somebody else may.
        return Recovery.REASSIGN
    return Recovery.REPLAN


def recovery_for(outcome: TaskOutcome) -> Recovery:
    """What an unsuccessful outcome calls for, failed or merely not accepted.

    A task the manager refused ended COMPLETED, so `classify` would read it as
    a success and answer a question nobody asked. The refusal already carries
    the distinction that matters: work blocked by permission may reach somebody
    else, work that simply did not happen needs a different plan.
    """
    if outcome.task.status is TaskStatus.COMPLETED:
        return Recovery.REASSIGN if outcome.acceptance.refused else Recovery.REPLAN
    return classify(outcome.task)


#: Failures where the same attempt is worth making again. Named by type, because
#: a message is not a contract and a retry decision made from one is a guess.
_TRANSIENT_ERRORS = frozenset(
    {"RateLimitError", "ProviderTimeoutError", "ProviderError", "ProviderUnavailableError"}
)


def _was_refused_a_tool(task: Task) -> bool:
    """Whether any call in this run never reached its tool.

    Read from the flag the executor writes, not from the sentence it writes
    beside it. This used to look for "may not use" in an observation's summary,
    which is the one thing recovery is not allowed to be decided by (§ recovery
    is chosen by the kind of failure, never by message text) - and it read only
    half of what refused means. `domain.tools.refusals` states the other half:
    a call is refused when the employee may not have the tool *or when a person
    said no*, and the approval gate writes the same flag for both.

    The cost of the narrow reading was measured rather than argued. A Phase 18
    run gave a comparison to the one employee that can run code, the person at
    the keyboard declined `code.run`, and the task came back refused - and was
    read as a bad plan, replanned into the same task, given to the same
    employee, and declined again. Reassignment would have handed it to somebody
    who could have done it by reading the two files.
    """
    observations = (task.result.output.get("observations") if task.result else None) or ()
    return any(
        isinstance(item, dict) and (item.get("details") or {}).get(REFUSED)
        for item in observations
    )


class Supervisor:
    """Carries a plan to the end, or to the point where it cannot go on."""

    def __init__(
        self,
        *,
        execution: TaskExecution,
        delegator: CapabilityDelegator,
        progress: ProgressSink | None = None,
        max_attempts: int = MAX_TASK_ATTEMPTS,
        max_parallel: int = MAX_PARALLEL_TASKS,
        escalation: EscalationAdvisor | None = None,
    ) -> None:
        self._execution = execution
        # None means no escalation: a failed task is retried, reassigned or
        # replanned exactly as before Phase 10.
        self._escalation = escalation
        self._delegator = delegator
        self._progress = progress or NullProgress()
        self._max_attempts = max_attempts
        self._max_parallel = max(1, max_parallel)

    async def run(
        self, plan: Plan, *, context: SharedContext | None = None, objective_id: UUID | None = None
    ) -> Supervision:
        """Run every task whose dependencies are met, until none are left.

        One wave at a time, and everything in a wave at once: `Plan.ready`
        returns tasks with no path between them, so running them together is
        what the plan already said and running them in file order was only ever
        an executor that had not caught up.
        """
        done: set[UUID] = set()
        outcomes: list[TaskOutcome] = []
        shortfall: list[str] = []
        recovery: Recovery | None = None
        # Every result that moves between employees moves through here, and
        # nothing else in this file builds a `SharedContext` for a task. That is
        # §86 as a structure rather than a rule people remember.
        coordinator = WorkforceCoordinator(plan, baseline=context or SharedContext())
        limit = asyncio.Semaphore(self._max_parallel)

        while True:
            ready = plan.ready(done)
            if not ready:
                break

            async def carry(planned: Task) -> TaskOutcome:
                async with limit:
                    return await self._carry(
                        planned,
                        coordinator.context_for(planned),
                        objective_id,
                        plan.requirements.get(planned.id),
                    )

            # Said out loud because Phase 18 could not answer "did anybody
            # actually work at the same time" from a recorded run. The width of
            # a wave is a property of the plan, not of this loop, and the loop
            # is the only place that knows it. Not a `ProgressEvent`: the six
            # kinds stay six, and this is for whoever reads the log of a
            # validation run rather than for the trace a person watches.
            log.info(
                "prometheus.wave",
                plan_id=str(plan.id),
                width=len(ready),
                limit=self._max_parallel,
                task_ids=[str(planned.id) for planned in ready],
            )
            wave = await asyncio.gather(*(carry(planned) for planned in ready))

            # Read in plan order, whatever order they finished in: a run that
            # reports its tasks in whichever one won a race is a run nobody can
            # compare with the last one.
            for planned, outcome in zip(ready, wave, strict=True):
                outcomes.append(outcome)
                if outcome.succeeded:
                    done.add(planned.id)
                    coordinator.record(planned.id, outcome.task)
                    continue

                # A failed task stops the plan: whatever depended on it would be
                # working from a result that does not exist. Its neighbours in
                # this wave are already finished and their results stand - they
                # never depended on it.
                if recovery is None:
                    recovery = recovery_for(outcome)
                shortfall.append(
                    f"{planned.goal} - {outcome.reason or 'did not complete'}"
                )
                log.info(
                    "prometheus.task_failed",
                    task_id=str(planned.id),
                    employee=outcome.employee,
                    status=outcome.task.status.value,
                    recovery=recovery.value,
                )
            if recovery is not None:
                break

        if recovery is None and plan.blocked(done):
            # Nothing failed and nothing is ready: the edges describe a cycle,
            # which is a bad decomposition rather than a bad run.
            recovery = Recovery.REPLAN
            shortfall.append(
                "The plan's dependencies cannot all be satisfied; some tasks never became ready."
            )

        return Supervision(
            plan=plan,
            outcomes=tuple(outcomes),
            recovery=recovery,
            shortfall=tuple(shortfall),
        )

    # --- One task -------------------------------------------------------------

    async def _carry(
        self,
        planned: Task,
        context: SharedContext,
        objective_id: UUID | None,
        requirement: Requirement | None = None,
    ) -> TaskOutcome:
        """Give one task to somebody, and try again if that is what the failure wants."""
        attempt = 1
        avoid: set[str] = set()
        outcome = await self._attempt(planned, context, objective_id, avoid, requirement)

        while not outcome.succeeded and attempt < self._max_attempts:
            recovery = recovery_for(outcome)
            if recovery is Recovery.REASSIGN:
                # Do not hand it back to the employee that could not reach it.
                avoid.add(outcome.employee)
            elif recovery is not Recovery.RETRY:
                break
            attempt += 1
            log.info(
                "prometheus.task_reattempt",
                task_id=str(planned.id),
                attempt=attempt,
                recovery=recovery.value,
            )
            # A second attempt is a new task, not the old one restarted: the
            # first is already in a terminal state, and a row that says FAILED
            # and later says COMPLETED is a row that lost the first attempt.
            outcome = await self._attempt(
                _next_attempt(planned, attempt), context, objective_id, avoid, requirement
            )

        cause = self._escalation_cause(outcome)
        if cause is not None:
            # One more attempt, on a stronger model, and only for a failure a
            # stronger model plausibly fixes. Not counted against the attempts
            # above: those were spent on the model routing chose, and this is
            # the one piece of evidence that the choice was too cheap.
            attempt += 1
            level = Escalation(level=1, cause=cause)
            log.info(
                "prometheus.task_escalated",
                task_id=str(planned.id),
                attempt=attempt,
                cause=cause.value,
            )
            await self._announce(
                planned,
                objective_id,
                "Trying again on a stronger model: the previous attempt failed with "
                f"{cause.value.lower().replace('_', ' ')}.",
                payload={"task_id": str(planned.id), "escalation": level.to_dict()},
            )
            outcome = await self._attempt(
                _next_attempt(planned, attempt),
                replace(context, data={**context.data, "escalation": level.to_dict()}),
                objective_id,
                avoid,
                requirement,
            )

        return outcome

    def _escalation_cause(self, outcome: TaskOutcome) -> escalations.EscalationCause | None:
        if outcome.succeeded or self._escalation is None:
            return None
        task = outcome.task
        cause = escalations.cause_of(
            completed=task.status is TaskStatus.COMPLETED,
            error_kind=task.error.kind if task.error else "",
            stopped_by=(task.result.output.get("stopped_by") if task.result else None),
            refused=outcome.acceptance.refused or _was_refused_a_tool(task),
            accepted=outcome.acceptance.accepted,
        )
        if cause is None:
            return None
        try:
            stage = {
                escalations.EscalationCause.PLANNING_FAILED: TaskKind.PLANNING,
                escalations.EscalationCause.VERIFICATION_REJECTED: TaskKind.VERIFICATION,
                escalations.EscalationCause.NOT_ACCEPTED: TaskKind.VERIFICATION,
                escalations.EscalationCause.STEP_BUDGET: TaskKind.EXECUTION,
            }[cause]
            if not self._escalation.stronger_available(stage):
                log.info("prometheus.no_stronger_model", task_id=str(task.id), cause=cause.value)
                return None
        except Exception as error:  # a catalog that cannot be read is no escalation
            log.warning("prometheus.escalation_unavailable", error=str(error))
            return None
        return cause

    async def _attempt(
        self,
        planned: Task,
        context: SharedContext,
        objective_id: UUID | None,
        avoid: set[str],
        requirement: Requirement | None = None,
    ) -> TaskOutcome:
        if planned.status is not TaskStatus.CREATED:
            return await self._resume(planned, objective_id)

        # `DelegationError` is deliberately not caught here. It means the machine
        # has no declared employee at all - a fact about the workforce, not
        # about this task - and every replanned attempt would end in the same
        # place. It belongs to whoever owns the objective.
        chosen, passed, why = await self._delegator.choose(
            planned, context=context, avoid=avoid, requirement=requirement
        )

        await self._announce(
            planned,
            objective_id,
            f"{chosen.name}: {planned.goal}",
            payload={"employee": chosen.name, "why": why, "task_id": str(planned.id)},
        )

        assignment = TaskAssignment.create(
            task_id=planned.id,
            employee_id=chosen.id,
            assigned_by=ActorKind.PROMETHEUS,
            assigned_by_id="prometheus",
            context=passed,
            workspace_id=planned.workspace_id,
        )
        finished = await self._execution.start(
            replace(
                planned,
                assigned_employee_id=chosen.id,
                assignment_reason=why,
            ),
            assignment,
        )
        # §88: the manager checks before accepting. Read off what the run did,
        # not asked of a second model - a claim of success with nothing behind
        # it is a fact about the record, and facts are cheaper than opinions.
        taken = accept(finished)
        if not taken.accepted:
            log.info(
                "prometheus.result_not_accepted",
                task_id=str(finished.id),
                employee=chosen.name,
                refused=taken.refused,
                reason=taken.reason,
            )
        return TaskOutcome(
            task=finished,
            employee=chosen.name,
            reason=taken.reason or _why(finished),
            acceptance=taken,
        )

    async def _resume(self, task: Task, objective_id: UUID | None) -> TaskOutcome:
        """Use persisted work instead of delegating or starting it again."""
        employee = self._delegator.employee_name(
            task.assigned_employee_id, task.workspace_id
        )
        if task.is_terminal:
            finished = task
        elif task.is_resumable:
            await self._announce(
                task,
                objective_id,
                f"Resuming {employee}: {task.goal}",
                payload={"employee": employee, "task_id": str(task.id), "resumed": True},
            )
            finished = await self._execution.resume(task)
        else:
            reason = (
                "The task was waiting on an external decision when the process stopped; "
                "the previous caller no longer exists."
            )
            return TaskOutcome(
                task=task,
                employee=employee,
                reason=reason,
                acceptance=Acceptance(accepted=False, reason=reason),
            )

        taken = accept(finished)
        return TaskOutcome(
            task=finished,
            employee=employee,
            reason=taken.reason or _why(finished),
            acceptance=taken,
        )

    async def _announce(
        self,
        task: Task,
        objective_id: UUID | None,
        message: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        try:
            await self._progress.emit(
                ProgressEvent(
                    task_id=task.id,
                    kind=ProgressKind.STAGE,
                    message=message,
                    objective_id=objective_id,
                    payload=dict(payload or {}),
                    workspace_id=task.workspace_id,
                )
            )
        except Exception as error:  # a watcher must not be able to fail a run
            log.warning("progress.emit_failed", error=str(error))


def _next_attempt(planned: Task, attempt: int) -> Task:
    """The same goal, as a fresh task hanging off the one that failed.

    Kept as a child rather than a sibling so the history reads as what it was -
    one piece of work, attempted twice - and so `tasks.parent_id` answers "what
    was this a retry of" without a join through the plan.
    """
    return replace(
        Task.create(
            planned.goal,
            workspace_id=planned.workspace_id,
            created_by=planned.created_by,
            priority=planned.priority,
            parent_id=planned.id,
        ),
        plan_id=planned.plan_id,
        attempts=attempt - 1,
    )


def _why(task: Task) -> str:
    if task.status is TaskStatus.COMPLETED:
        return ""
    if task.error:
        return f"{task.error.kind}: {task.error.message}"
    return f"ended {task.status.value.lower()}"
