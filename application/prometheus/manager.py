"""Prometheus: the user states a goal, and this decides what happens to it.

`WorkforceManager` in one class. The order is fixed and each step is a component
of its own, because each is a different kind of judgement and they fail in
different ways:

    read it -> (answer it) -> plan it -> delegate and supervise -> check it
             -> (replan once) -> write the answer

Two of those are in brackets, and both are the point of the phase.

**Answering directly.** A request that needs no work gets an answer, not a plan
(§7.5). Decomposition is a means; an objective broken into one task whose whole
content is the question already asked has cost an employee run to restate it.

That route is checked before it is taken, and the first full validation run is
why. "Read the notes in `notes/` and leave me `notes/summary.md`" was read as
needing no work and closed with one sentence: no task, no tool, no file, and a
report of success. So a direct answer now faces the same verifier, against the
same criteria written before the reading, as delegated work does - and a claim
of "no work needed" that its own answer cannot satisfy becomes work, with what
the check found missing handed to the planner. Deciding what a request takes is
a judgement like any other, and the platform does not take its own word for a
judgement anywhere else.

**Replanning once.** A verdict against the objective's own acceptance criteria
can send the work back through planning, told what was missing - once. A second
rejection usually means the criteria cannot be met by this workforce, and a
third plan spends another budget finding that out again. What is left is
escalated to the user, with what was tried (§7.11).

**Reconciling.** Several people on one objective can report two things that
cannot both be true. Blending them into one confident paragraph is the worst
answer available, so the reports are checked against each other before the
answer is written: Prometheus settles what the evidence settles and escalates the
rest, with both sides named (§87). Nothing runs for a single-task plan - one
report cannot disagree with itself.

**Remembering.** What Prometheus reads out of a request - how the user wants things
done here - outlives the request, and what a workspace has already learned is
passed down to the tasks it delegates rather than kept for itself. Memory is
optional and reached through a contract, so a manager without one plans exactly
as it did before (§9.4).

What this class does *not* do is as deliberate:

* it never names an employee - candidates come from `EmployeeRegistry`;
* it never runs a task - `TaskExecution` does, and Prometheus holds only the contract;
* it never resolves an approval (§7.10). An irreversible action inside a
  delegated task stops at the same gate it would have stopped at if the user had
  asked the employee directly, and the person who answers it is a person. Prometheus
  can explain what it is for; it cannot say yes to its own work.
"""

from __future__ import annotations

from dataclasses import replace

import structlog

from application.knowledge.workspace import WorkspaceKnowledge
from application.memory.workspace import WorkspaceMemory
from application.prometheus.intent import IntentReader
from application.prometheus.planner import ObjectivePlanner
from application.prometheus.reconciliation import Reconciler, Reconciliation
from application.prometheus.supervisor import Recovery, Supervision, Supervisor
from application.prometheus.synthesis import NOTHING_WAS_DONE, Synthesizer, actions, describe
from application.prometheus.verification import ObjectiveVerifier
from domain.employees.protocols import EmployeeRegistry
from domain.errors import DelegationError
from domain.tasks.progress import NullProgress, ProgressEvent, ProgressKind, ProgressSink
from domain.workforce.assignment import SharedContext
from domain.workforce.intent import Intent
from domain.workforce.protocols import (
    Objective,
    ObjectiveResult,
    ObjectiveStatus,
    Plan,
    PlanStatus,
)
from domain.workforce.repository import ObjectiveRepository, PlanRepository

log = structlog.get_logger(__name__)

#: How many plans one objective is worth. The second is told what the first
#: missed; a third would be told the same thing again.
MAX_PLAN_REVISIONS = 2


class PrometheusManager:
    """Implements `domain.workforce.protocols.WorkforceManager`."""

    def __init__(
        self,
        *,
        intent: IntentReader,
        planner: ObjectivePlanner,
        supervisor: Supervisor,
        verifier: ObjectiveVerifier,
        synthesizer: Synthesizer,
        registry: EmployeeRegistry,
        reconciler: Reconciler | None = None,
        objectives: ObjectiveRepository,
        plans: PlanRepository,
        progress: ProgressSink | None = None,
        max_revisions: int = MAX_PLAN_REVISIONS,
        memory: WorkspaceMemory | None = None,
        knowledge: WorkspaceKnowledge | None = None,
    ) -> None:
        self._intent = intent
        self._planner = planner
        self._supervisor = supervisor
        self._verifier = verifier
        self._synthesizer = synthesizer
        self._registry = registry
        # Optional in the same way memory is: a machine without one runs the
        # Phase 7 loop, which is what a single-task plan does anyway.
        self._reconciler = reconciler
        self._objectives = objectives
        self._plans = plans
        self._progress = progress or NullProgress()
        self._max_revisions = max_revisions
        self._memory = memory
        # Optional the way memory is, and read at the same moment: a question
        # whose answer is in a document the user added has to reach the reading
        # of the request, or it is answered "I do not have that" before any task
        # exists to retrieve it. That is what the first Phase 15 validation run
        # did (validation/tasks/phase-15-*).
        self._knowledge = knowledge

    # --- The whole of it ------------------------------------------------------

    async def receive(self, request: str, workspace_id=None, conversation_id=None) -> Objective:
        """Turn a sentence into a recorded objective, before anything is done.

        Written down first, like a task is: a process killed one second later
        still leaves a record of what was asked, and the user's own words are
        kept beside whatever Prometheus made of them.

        `conversation_id` is a label carried by the record, nothing more. The
        manager does not read the thread and does not behave differently inside
        one: what an earlier request established reaches this one through
        memory, which is recalled per objective and works the same whether the
        request arrived from a page, the CLI or a schedule. A manager that
        branched on the presence of a conversation would be a second way to run
        work, and the interface it favoured would be the only one that worked
        properly.
        """
        objective = Objective.create(
            request.strip(),
            **({"workspace_id": workspace_id} if workspace_id is not None else {}),
            **({"conversation_id": conversation_id} if conversation_id is not None else {}),
        )
        await self._objectives.save(objective)
        log.info("prometheus.objective_received", objective_id=str(objective.id))
        return objective

    async def handle_objective(self, objective: Objective) -> ObjectiveResult:
        """Carry one objective to an answer.

        A failure nothing below anticipated - a model server that is not
        running, a store that went away - closes the record as FAILED and is
        announced before it is raised again. Raising alone was the whole of it
        until a request typed into the window with the local model server
        stopped: the error left a background coroutine nobody awaited, the row
        stayed RECEIVED, and every interface showed "reading your request" for
        good. Re-raised rather than swallowed, because the callers that are
        waiting - the CLI, a schedule, the validation harness - classify it by
        its type.
        """
        try:
            return await self._carry(objective)
        except Exception as error:
            await self._fail(objective, error)
            raise

    async def _fail(self, objective: Objective, error: Exception) -> None:
        log.warning(
            "prometheus.objective_failed",
            objective_id=str(objective.id),
            error=f"{type(error).__name__}: {error}",
        )
        try:
            # Read back rather than taken from the argument: the stages above
            # saved what the request was understood to mean, and writing the
            # copy that was handed in would erase it.
            current = await self._objectives.get(objective.id) or objective
            if current.is_terminal:
                return
            await self._finish(
                current,
                ObjectiveStatus.FAILED,
                summary=f"This could not be finished: {error}",
            )
        except Exception as closing:  # the original error is the one worth raising
            log.warning(
                "prometheus.objective_not_closed",
                objective_id=str(objective.id),
                error=f"{type(closing).__name__}: {closing}",
            )

    async def _carry(self, objective: Objective) -> ObjectiveResult:
        workforce = self._registry.list(objective.workspace_id)
        await self._announce(objective, "Working out what you are asking for.")

        # Recalled before the request is read, not after it is planned. A
        # request written against the last one - "do the same for the returns
        # folder" - cannot be understood without what the last one was, and
        # every stage below reads better for having it.
        remembered, documents = await self._remembered(objective)

        intent = await self._intent.read(
            objective.text, workforce, remembered=remembered, documents=documents
        )
        objective = self._understood(objective, intent)
        await self._objectives.save(objective)
        if self._memory is not None and intent.preferences:
            # A preference stated in passing - "always in Markdown", "never
            # touch the originals" - is stated once and expected to hold. It is
            # kept here, where it was read, rather than at the end, where a
            # failed objective would take it down with it.
            #
            # `intent.constraints` is deliberately not kept: those describe this
            # request, and remembering them as standing preferences is what sent
            # the second validation run back to the first one's folder.
            await self._memory.remember_preferences(
                intent.preferences,
                source=objective.text,
                workspace_id=objective.workspace_id,
            )

        rejected: tuple[str, ...] = ()
        if intent.is_conversation:
            # Nothing to do and nothing written down to check: a greeting, a
            # question about Prometheus, a fact that does not change. The
            # verifier used to see these too, and a local model judging them
            # against "nothing was done" rejected every one - "Hello" became a
            # plan, and "what is the capital of France" became work that could
            # not be started. A reading that names a standard is still checked
            # against it below; this is only the case where there is none.
            return await self._answer_directly(objective, intent)
        if intent.is_answerable_directly:
            # The evidence for a direct answer is that there is none. Saying so
            # is the whole of Phase 18's fix here: three runs in a row answered
            # this request out of a memory of the last one, and a verifier shown
            # only the sentence had nothing to disagree with.
            verdict = await self._verifier.verify(
                objective, intent.answer, actions=NOTHING_WAS_DONE
            )
            if verdict.passed:
                return await self._answer_directly(objective, intent)
            # It said this needed no work and then could not meet the standard
            # it had just written. The standard is the more specific claim, and
            # the one a plan can act on.
            rejected = verdict.missing or (verdict.reason,)
            log.info(
                "prometheus.direct_answer_rejected",
                objective_id=str(objective.id),
                missing=list(rejected),
            )

        try:
            return await self._work(
                objective, intent, remembered + documents, feedback=rejected
            )
        except DelegationError as error:
            # Nothing to delegate to is the user's to fix, not something to
            # replan around: every plan would end in the same place.
            return await self._finish(
                objective,
                ObjectiveStatus.ESCALATED,
                summary=str(error),
                missing=("someone declared who can do this work",),
            )

    # --- The two routes -------------------------------------------------------

    async def _answer_directly(
        self, objective: Objective, intent: Intent
    ) -> ObjectiveResult:
        """No plan, no employee, no tools (§7.5)."""
        log.info("prometheus.answered_directly", objective_id=str(objective.id))
        if self._memory is not None:
            # Nothing else records this: no task ran, so without it the only
            # trace that the question was asked is the objective row.
            await self._memory.remember_answer(
                objective.text, intent.answer, workspace_id=objective.workspace_id
            )
        return await self._finish(
            objective,
            ObjectiveStatus.DONE,
            summary=intent.answer,
            output={"delegated": False, "restatement": intent.restatement},
        )

    async def _work(
        self,
        objective: Objective,
        intent: Intent,
        remembered: tuple[str, ...] = (),
        *,
        feedback: tuple[str, ...] = (),
    ) -> ObjectiveResult:
        """`feedback` is non-empty when a direct answer was tried and rejected:
        the first plan is then told what the sentence failed to cover, rather
        than starting from nothing and possibly missing the same thing."""
        workforce = self._registry.list(objective.workspace_id)
        objective = objective.to(ObjectiveStatus.PLANNING)
        await self._objectives.save(objective)

        supervision: Supervision | None = None
        plan: Plan | None = None
        cost = 0.0

        for revision in range(1, self._max_revisions + 1):
            plan = await self._planner.plan(
                objective,
                workforce,
                restatement=intent.restatement,
                revision=revision,
                feedback=feedback,
                remembered=remembered,
            )
            await self._plans.save(plan)
            await self._announce(
                objective,
                plan.rationale or f"{len(plan.tasks)} task(s) planned.",
                kind=ProgressKind.PLAN,
                payload={
                    "plan_id": str(plan.id),
                    "revision": revision,
                    "tasks": [
                        {"id": str(task.id), "goal": task.goal} for task in plan.tasks
                    ],
                },
            )

            objective = objective.to(ObjectiveStatus.RUNNING)
            await self._objectives.save(objective)
            plan = plan.to(PlanStatus.RUNNING)
            await self._plans.save(plan)

            supervision = await self._supervisor.run(
                plan,
                context=SharedContext(
                    facts=remembered, constraints=tuple(intent.acceptance_criteria)
                ),
                objective_id=objective.id,
            )
            cost += supervision.cost_usd

            # Before judging the work against the objective: two reports that
            # contradict each other are a different failure from work that fell
            # short, and a verdict on the pair of them is a verdict on a
            # contradiction. §87 comes first.
            agreement = await self._reconcile(objective, supervision)
            if agreement.escalate:
                await self._plans.save(plan.to(PlanStatus.FAILED))
                return await self._escalate(
                    objective, supervision, agreement.conflicts, cost=cost
                )

            verdict = await self._verifier.verify(
                objective,
                describe(supervision.outcomes),
                actions=actions(supervision.outcomes),
            )
            await self._plans.save(
                plan.to(PlanStatus.DONE if verdict.passed else PlanStatus.FAILED)
            )

            if verdict.passed:
                return await self._deliver(
                    objective, supervision, cost=cost, resolution=agreement.resolution
                )

            feedback = verdict.missing or (verdict.reason,)
            log.info(
                "prometheus.objective_rejected",
                objective_id=str(objective.id),
                revision=revision,
                missing=list(feedback),
            )
            if revision < self._max_revisions and supervision.recovery is not Recovery.GIVE_UP:
                await self._plans.save(plan.superseded())
                await self._announce(
                    objective,
                    "That did not meet what you asked for. Planning a second attempt.",
                    payload={"missing": list(feedback)},
                )
                continue
            break

        # Out of revisions. Hand back what there is, and say what is missing.
        return await self._escalate(objective, supervision, feedback, cost=cost)

    # --- Endings --------------------------------------------------------------

    async def _deliver(
        self,
        objective: Objective,
        supervision: Supervision,
        *,
        cost: float,
        resolution: str = "",
    ) -> ObjectiveResult:
        summary = await self._synthesizer.synthesize(
            objective, supervision.outcomes, resolution=resolution
        )
        return await self._finish(
            objective,
            ObjectiveStatus.DONE,
            summary=summary,
            output=_evidence(supervision),
            cost_usd=cost,
        )

    async def _escalate(
        self,
        objective: Objective,
        supervision: Supervision | None,
        missing: tuple[str, ...],
        *,
        cost: float,
    ) -> ObjectiveResult:
        """Give the user what there is, plus what was tried and what is absent.

        Escalation is not failure with a nicer name: the work that succeeded is
        still handed over, and the history of attempts is what makes the request
        actionable rather than a shrug (§7.11).
        """
        outcomes = supervision.outcomes if supervision else ()
        summary = (
            await self._synthesizer.synthesize(objective, outcomes, missing=missing)
            if outcomes
            else "Nothing could be produced for this objective."
        )
        log.info(
            "prometheus.escalated",
            objective_id=str(objective.id),
            attempts=len(outcomes),
            missing=list(missing),
        )
        return await self._finish(
            objective,
            ObjectiveStatus.ESCALATED,
            summary=summary,
            output=_evidence(supervision) if supervision else {},
            missing=missing,
            cost_usd=cost,
        )

    async def _finish(
        self,
        objective: Objective,
        status: ObjectiveStatus,
        *,
        summary: str,
        output: dict[str, object] | None = None,
        missing: tuple[str, ...] = (),
        cost_usd: float = 0.0,
    ) -> ObjectiveResult:
        result = ObjectiveResult(
            objective_id=objective.id,
            summary=summary,
            status=status,
            output=dict(output or {}),
            missing=missing,
            cost_usd=cost_usd,
        )
        await self._objectives.save(objective.to(status, result))
        await self._announce(
            objective,
            summary,
            kind=ProgressKind.RESULT,
            payload={"status": status.value, "missing": list(missing), "cost_usd": cost_usd},
        )
        log.info(
            "prometheus.objective_finished",
            objective_id=str(objective.id),
            status=status.value,
            cost_usd=round(cost_usd, 6),
        )
        return result

    # --- Internals ------------------------------------------------------------

    async def _reconcile(
        self, objective: Objective, supervision: Supervision
    ) -> Reconciliation:
        """Can everything the team reported be true at once?

        Only the accepted results are compared. A task that failed, or that the
        manager refused, is not a second opinion - and asking whether a result
        contradicts a failure is asking a question with no answer.
        """
        if self._reconciler is None:
            return Reconciliation()
        reports = tuple(
            outcome.task.result.summary
            for outcome in supervision.outcomes
            if outcome.succeeded and outcome.task.result
        )
        return await self._reconciler.reconcile(objective, reports)

    async def _remembered(
        self, objective: Objective
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """What this workspace knows that bears on this request.

        Two sources, one query each, read once per objective and used
        everywhere: by the reading of the request, by the decomposition, and by
        every task in the plan. Reading them per stage would cost the same
        queries three times and could answer them three different ways.
        """
        remembered: tuple[str, ...] = ()
        documents: tuple[str, ...] = ()
        if self._memory is not None:
            remembered = await self._memory.context_for(
                objective.text, workspace_id=objective.workspace_id
            )
        if self._knowledge is not None:
            documents = await self._knowledge.context_for(
                objective.text, workspace_id=objective.workspace_id
            )
        # Kept apart all the way down. What the platform noted is a lead that
        # may be stale; what the user brought is evidence with a source on it,
        # and the two are rendered differently wherever they are shown.
        return remembered, documents

    @staticmethod
    def _understood(objective: Objective, intent: Intent) -> Objective:
        """What Prometheus read, recorded next to what was said - never instead of it."""
        return replace(
            objective,
            constraints={**objective.constraints, **intent.constraints},
            acceptance_criteria=intent.acceptance_criteria,
        )

    async def _announce(
        self,
        objective: Objective,
        message: str,
        *,
        kind: ProgressKind = ProgressKind.STAGE,
        payload: dict[str, object] | None = None,
    ) -> None:
        try:
            await self._progress.emit(
                ProgressEvent(
                    task_id=objective.id,
                    objective_id=objective.id,
                    kind=kind,
                    message=message,
                    payload=dict(payload or {}),
                    workspace_id=objective.workspace_id,
                )
            )
        except Exception as error:  # a watcher must not be able to fail a run
            log.warning("progress.emit_failed", error=str(error))


def _evidence(supervision: Supervision) -> dict[str, object]:
    """What the answer rests on, so it can be checked rather than believed."""
    progress = supervision.progress
    return {
        "delegated": True,
        "plan_id": str(supervision.plan.id),
        "revision": supervision.plan.revision,
        "tasks": [
            {
                "id": str(outcome.task.id),
                "goal": outcome.task.goal,
                "employee": outcome.employee,
                "status": outcome.task.status.value,
                "cost_usd": round(outcome.task.cost_usd, 6),
            }
            for outcome in supervision.outcomes
        ],
        "completed": progress.completed,
        "failed": progress.failed,
    }
