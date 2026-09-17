"""The manager, end to end, on scripted models and a workforce that never runs.

The Definition of Done for Phase 7 is a user stating a goal in one sentence and
getting a verified result without ever addressing an employee. These are that
sentence, taken apart: what Prometheus does with a request that needs no work, with one
that needs several people, with a result that does not meet what was asked, and
with a machine that has nobody to give work to.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from application.prometheus.delegation import CapabilityDelegator
from application.prometheus.intent import IntentReader
from application.prometheus.manager import PrometheusManager
from application.prometheus.planner import ObjectivePlanner
from application.prometheus.supervisor import Supervisor
from application.prometheus.synthesis import NOTHING_WAS_DONE, Synthesizer, actions
from application.prometheus.verification import ObjectiveVerifier
from domain.errors import ProviderUnavailableError
from domain.observability.models import SpanKind
from domain.tasks.task import Task, TaskCreatedBy, TaskResult, TaskStatus
from domain.workforce.protocols import Objective, ObjectiveStatus, Plan, PlanStatus
from infrastructure.persistence.objective_repository import InMemoryObjectiveRepository
from infrastructure.persistence.plan_repository import InMemoryPlanRepository
from infrastructure.persistence.trace_repository import InMemoryTraceRepository
from infrastructure.progress.broadcaster import InMemoryProgressBroadcaster
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.workforce import FakeRegistry, RecordingExecution, failing

READER = definition("reader", tools=frozenset({"fs.read"}))
WRITER = definition("writer", tools=frozenset({"fs.write"}))


def intent(**overrides) -> str:
    return json.dumps(
        {
            "restatement": "do the thing",
            "constraints": {},
            "preferences": [],
            "acceptance_criteria": ["the thing is done"],
            "needs_work": True,
            "answer": "",
            **overrides,
        }
    )


def plan(*goals: str, **extra) -> str:
    return json.dumps(
        {
            "rationale": "because",
            "tasks": [{"id": f"t{i}", "goal": goal} for i, goal in enumerate(goals)],
            **extra,
        }
    )


def verdict(passed: bool, *missing: str) -> str:
    return json.dumps(
        {"passed": passed, "reason": "checked", "missing": list(missing)}
    )


def build(
    *,
    script: list,
    delegation: list | None = None,
    workforce=(READER,),
    execution: RecordingExecution | None = None,
    progress: InMemoryProgressBroadcaster | None = None,
    objectives: InMemoryObjectiveRepository | None = None,
    plans: InMemoryPlanRepository | None = None,
    max_revisions: int = 2,
    memory=None,
    knowledge=None,
    reconciler=None,
    session=None,
    traces: InMemoryTraceRepository | None = None,
    #: Supply one to read back what each stage was actually told.
    llm: FakeLLM | None = None,
) -> tuple[
    PrometheusManager, RecordingExecution, InMemoryObjectiveRepository, InMemoryPlanRepository
]:
    """One model for every stage, answering from a single script in order.

    Sharing the client is what makes the script readable as a story: read it,
    plan it, choose who, check it, write the answer.
    """
    llm = llm or FakeLLM([reply(item) if isinstance(item, str) else item for item in script])
    # Delegation gets its own client when more than one employee is declared, so
    # that "who should do this" does not have to be interleaved into the story
    # above at whatever point the supervisor happens to ask.
    chooser = FakeLLM([reply(item) for item in delegation or ()]) if delegation else llm
    registry = FakeRegistry(*(workforce if workforce is not None else (READER,)))
    runs = execution or RecordingExecution()
    objective_store = objectives or InMemoryObjectiveRepository()
    plan_store = plans or InMemoryPlanRepository()
    manager = PrometheusManager(
        intent=IntentReader(llm),
        planner=ObjectivePlanner(llm),
        supervisor=Supervisor(
            execution=runs,
            delegator=CapabilityDelegator(chooser, registry),
            max_attempts=1,
        ),
        verifier=ObjectiveVerifier(llm),
        synthesizer=Synthesizer(llm),
        reconciler=reconciler,
        registry=registry,
        objectives=objective_store,
        plans=plan_store,
        progress=progress or InMemoryProgressBroadcaster(),
        max_revisions=max_revisions,
        memory=memory,
        knowledge=knowledge,
        session=session,
        traces=traces,
    )
    return manager, runs, objective_store, plan_store


# --- The happy path -----------------------------------------------------------


async def test_one_sentence_becomes_a_verified_answer() -> None:
    manager, execution, objectives, plans = build(
        script=[
            intent(),
            plan("Read the notes"),
            verdict(True),
            "The notes say the answer is 41.",
        ]
    )

    objective = await manager.receive("Tell me what the notes say")
    result = await manager.handle_objective(objective)

    assert result.status is ObjectiveStatus.DONE
    assert result.summary == "The notes say the answer is 41."
    assert execution.goals == ["Read the notes"]

    # And it is all on the record: the objective, its reading, its plan.
    stored = await objectives.get(objective.id)
    assert stored is not None and stored.status is ObjectiveStatus.DONE
    assert stored.acceptance_criteria == ("the thing is done",)
    assert stored.result is not None and stored.result.summary == result.summary
    assert stored.text == "Tell me what the notes say", "the user's words are kept verbatim"

    revisions = await plans.for_objective(objective.id)
    assert [p.revision for p in revisions] == [1]
    assert revisions[0].status.value == "DONE"


async def test_restart_reuses_completed_work_and_resumes_only_the_interrupted_task() -> None:
    execution = RecordingExecution()
    objectives = InMemoryObjectiveRepository()
    plans = InMemoryPlanRepository()
    traces = InMemoryTraceRepository()
    manager, _, _, _ = build(
        script=[verdict(True), "The recovered work is complete."],
        execution=execution,
        objectives=objectives,
        plans=plans,
        traces=traces,
    )
    objective = replace(
        Objective.create("Read and summarise the notes"),
        status=ObjectiveStatus.RUNNING,
        acceptance_criteria=("the notes are summarised",),
    )
    first = replace(
        Task.create("Read the notes", created_by=TaskCreatedBy.PROMETHEUS),
        status=TaskStatus.COMPLETED,
        assigned_employee_id=READER.id,
        result=TaskResult(summary="The notes contain three items."),
    )
    second = replace(
        Task.create("Summarise the notes", created_by=TaskCreatedBy.PROMETHEUS),
        status=TaskStatus.RUNNING,
        assigned_employee_id=READER.id,
    )
    saved = Plan.create(
        objective.id,
        tasks=(first, second),
        dependencies=((second.id, first.id),),
        status=PlanStatus.RUNNING,
    )
    await objectives.save(objective)
    await plans.save(saved)

    result = await manager.resume_objective(objective)

    assert result.status is ObjectiveStatus.DONE
    assert result.summary == "The recovered work is complete."
    assert [task.id for task, _ in execution.started] == [second.id]
    assert execution.started[0][0].status is TaskStatus.RUNNING
    trace = await traces.get(objective.id)
    assert trace is not None
    [resume] = [event for event in trace.events if event.kind is SpanKind.RESUME]
    assert resume.trace_id == objective.id
    assert resume.parent_id is not None


async def test_the_answer_carries_the_evidence_behind_it() -> None:
    manager, _, _, _ = build(
        script=[intent(), plan("Read it", "Write it"), verdict(True), "Done."],
        # Two employees means the delegator asks, and two tasks means twice.
        delegation=[json.dumps({"employee": "reader"}), json.dumps({"employee": "writer"})],
        workforce=(READER, WRITER),
    )

    result = await manager.handle_objective(await manager.receive("Do two things"))

    assert result.output["delegated"] is True
    assert [task["employee"] for task in result.output["tasks"]] == ["reader", "writer"]
    assert result.output["completed"] == 2
    assert result.cost_usd > 0


# --- Not everything needs the workforce (§7.5) --------------------------------


async def test_a_question_is_answered_without_a_plan_or_an_employee() -> None:
    manager, execution, _, plans = build(
        script=[
            intent(needs_work=False, answer="It keeps the database in one file."),
            # A direct answer is checked against the objective's criteria before
            # anyone sees it, exactly as delegated work is.
            verdict(True),
        ]
    )

    result = await manager.handle_objective(await manager.receive("What is SQLite?"))

    assert result.status is ObjectiveStatus.DONE
    assert result.summary == "It keeps the database in one file."
    assert execution.started == [], "nobody was given work"
    assert await plans.for_objective(result.objective_id) == [], "and nothing was decomposed"
    assert result.output["delegated"] is False


async def test_talk_is_answered_without_being_checked_against_nothing() -> None:
    """Found in the window: a local model judged "Hello" and "what is the
    capital of France" against "nothing was done" and rejected both, so a
    greeting became a plan. With no criteria there is nothing to check."""
    manager, execution, _, plans = build(
        script=[intent(needs_work=False, answer="Hello! What can I do?", acceptance_criteria=[])]
    )

    result = await manager.handle_objective(await manager.receive("Hello"))

    assert result.status is ObjectiveStatus.DONE
    assert result.summary == "Hello! What can I do?"
    assert execution.started == []
    assert await plans.for_objective(result.objective_id) == []


async def test_an_answer_that_cannot_meet_the_criteria_becomes_work() -> None:
    """Phase 11 finding 1, as a test.

    "Read the notes and leave me a summary file" was read as needing no work
    and closed with one sentence: nothing was done and the run reported
    success. Deciding what a request takes is a judgement, and it is checked
    like every other judgement here.
    """
    manager, execution, _, plans = build(
        script=[
            intent(
                needs_work=False,
                answer="Here is a summary of your notes.",
                acceptance_criteria=["notes/summary.md exists and summarises the notes"],
            ),
            verdict(False, "no file was written"),
            plan("Read notes/ and write notes/summary.md"),
            verdict(True),
            "Written.",
        ]
    )

    result = await manager.handle_objective(
        await manager.receive("Read the notes in notes/ and leave me notes/summary.md")
    )

    assert result.status is ObjectiveStatus.DONE
    assert execution.started, "the sentence did not stand in for the file"
    assert await plans.for_objective(result.objective_id) != []
    assert result.output["delegated"] is True


async def test_the_planner_is_told_what_the_direct_answer_missed() -> None:
    """The rejected claim is the first plan's best lead, not a discarded one."""
    llm = FakeLLM(
        [
            reply(intent(needs_work=False, answer="Here you go.")),
            reply(verdict(False, "no file was written")),
            reply(plan("Write the file")),
            reply(verdict(True)),
            reply("Written."),
        ]
    )
    manager, _, _, _ = build(script=[], llm=llm)

    await manager.handle_objective(await manager.receive("Leave me a summary file"))

    planning = llm.requests[2].messages[0].content
    assert "no file was written" in planning


# --- When it does not meet what was asked -------------------------------------


async def test_a_rejected_result_is_replanned_once_and_then_escalated() -> None:
    manager, execution, _, plans = build(
        script=[
            intent(),
            plan("Find twenty things"),
            verdict(False, "only eleven were found"),
            plan("Find twenty things, properly"),
            verdict(False, "only eleven were found"),
            "Here are the eleven I found.",
        ]
    )

    result = await manager.handle_objective(await manager.receive("Find twenty things"))

    assert result.status is ObjectiveStatus.ESCALATED
    assert result.missing == ("only eleven were found",)
    assert "eleven" in result.summary
    assert len(execution.started) == 2, "it was planned and run twice, not more"

    revisions = await plans.for_objective(result.objective_id)
    assert [p.revision for p in revisions] == [2, 1]
    assert revisions[1].status.value == "SUPERSEDED", "the first plan is kept, not overwritten"


async def test_the_second_plan_is_told_what_the_first_one_missed() -> None:
    manager, _, _, _ = build(
        script=[
            intent(),
            plan("Find twenty things"),
            verdict(False, "only eleven were found"),
            plan("Find nine more"),
            verdict(True),
            "Twenty things.",
        ]
    )

    result = await manager.handle_objective(await manager.receive("Find twenty things"))

    assert result.status is ObjectiveStatus.DONE


async def test_work_that_failed_is_still_handed_over_with_what_is_missing() -> None:
    """Escalation is not failure with a nicer name (§7.11)."""
    manager, _, objectives, _ = build(
        script=[
            intent(),
            plan("Fetch the source"),
            verdict(False, "nothing was fetched"),
            plan("Fetch the source again"),
            verdict(False, "nothing was fetched"),
            "I could not fetch anything.",
        ],
        execution=RecordingExecution(failing()),
    )

    result = await manager.handle_objective(await manager.receive("Fetch it"))

    assert result.status is ObjectiveStatus.ESCALATED
    assert result.missing == ("nothing was fetched",)
    stored = await objectives.get(result.objective_id)
    assert stored is not None and stored.finished_at is not None


async def test_nobody_to_delegate_to_is_escalated_not_replanned() -> None:
    """Every plan would end in the same place, so a second one is waste."""
    manager, _, _, plans = build(
        script=[intent(), plan("Do it")], workforce=()
    )

    result = await manager.handle_objective(await manager.receive("Do it"))

    assert result.status is ObjectiveStatus.ESCALATED
    assert "employees/" in result.summary
    assert len(await plans.for_objective(result.objective_id)) == 1


async def test_a_failure_nothing_anticipated_closes_the_objective_and_says_so() -> None:
    """Found in the window: the model server was not running, the error left a
    coroutine nobody awaited, and the request read "working out what you are
    asking for" for good."""
    progress = InMemoryProgressBroadcaster()
    manager, _, objectives, _ = build(
        script=[ProviderUnavailableError("the local model server is not answering")],
        progress=progress,
    )
    objective = await manager.receive("hello")

    with pytest.raises(ProviderUnavailableError):
        await manager.handle_objective(objective)

    stored = await objectives.get(objective.id)
    assert stored is not None
    assert stored.status is ObjectiveStatus.FAILED
    assert stored.finished_at is not None
    assert stored.result is not None and "not answering" in stored.result.summary
    last = progress.recent(objective.id)[-1]
    assert last.kind.value == "RESULT"
    assert last.payload["status"] == "FAILED"


# --- What the interface sees --------------------------------------------------


async def test_the_manager_announces_its_own_progress() -> None:
    progress = InMemoryProgressBroadcaster()
    manager, _, _, _ = build(
        script=[intent(), plan("Read it"), verdict(True), "Done."], progress=progress
    )

    objective = await manager.receive("Read the notes")
    await manager.handle_objective(objective)

    events = progress.recent(objective.id)
    kinds = [event.kind.value for event in events]
    assert kinds[0] == "STAGE"
    assert "PLAN" in kinds
    assert kinds[-1] == "RESULT"
    assert all(event.objective_id == objective.id for event in events)
    # The plan event names the tasks, which is how a watcher learns to follow them.
    planned = next(event for event in events if event.kind.value == "PLAN")
    assert planned.payload["tasks"] and planned.payload["tasks"][0]["goal"] == "Read it"


# --- What the first real run of this phase taught -----------------------------


async def test_a_verdict_that_passes_while_naming_something_missing_does_not_pass() -> None:
    """Found in the validation run: the model said yes and listed a gap.

    Reading it either way is a choice, so it is read the safe way - the list is
    the more specific claim, and the one a second attempt can act on.
    """
    manager, _, _, _ = build(
        script=[
            intent(),
            plan("Do it"),
            json.dumps({"passed": True, "reason": "looks fine", "missing": ["the count"]}),
            plan("Do it properly"),
            verdict(True),
            "Done properly.",
        ]
    )

    result = await manager.handle_objective(await manager.receive("Do it"))

    assert result.status is ObjectiveStatus.DONE, "it replanned rather than shipping the gap"
    assert result.summary == "Done properly."


async def test_a_verdict_saying_nothing_is_missing_is_not_a_contradiction() -> None:
    """A model that writes "none" is agreeing with itself, not disagreeing."""
    manager, execution, _, _ = build(
        script=[
            intent(),
            plan("Do it"),
            json.dumps({"passed": True, "reason": "fine", "missing": ["none", "  "]}),
            "Done.",
        ]
    )

    result = await manager.handle_objective(await manager.receive("Do it"))

    assert result.status is ObjectiveStatus.DONE
    assert len(execution.started) == 1, "it was not replanned"


async def test_with_no_criteria_the_request_itself_becomes_the_standard() -> None:
    """Also from the validation run: a weak reading must not switch the check off.

    Passing by default when no criteria were written down would make Prometheus's own
    verification a no-op exactly when comprehension had been weakest.
    """
    manager, _, _, _ = build(
        script=[
            intent(acceptance_criteria=[]),
            plan("Do it"),
            verdict(False, "it did not answer the question that was asked"),
            plan("Do it properly"),
            verdict(False, "it did not answer the question that was asked"),
            "Here is what I managed.",
        ]
    )

    result = await manager.handle_objective(await manager.receive("Answer my question"))

    assert result.status is ObjectiveStatus.ESCALATED
    assert result.missing == ("it did not answer the question that was asked",)


# --- What the verifier is shown -----------------------------------------------


async def test_a_direct_answer_is_verified_against_having_done_nothing() -> None:
    """Phase 18's worst finding, as a property of the prompt the check gets.

    Three consecutive runs of the same request were answered out of a memory of
    the previous one: the document had been written the day before, the answer
    said what was in it, the file had been deleted before the run started, and
    every criterion read as met. What separated that from a real result was not
    in the text the verifier was shown.
    """
    llm = FakeLLM(
        [
            reply(intent(needs_work=False, answer="It compares as follows.")),
            reply(verdict(True)),
        ]
    )
    manager, _, _, _ = build(script=[], llm=llm)

    objective = await manager.receive("Read both and leave me a comparison file")
    await manager.handle_objective(objective)

    checking = llm.requests[-1].messages[0].content
    assert "What was actually done" in checking
    assert NOTHING_WAS_DONE in checking


def test_the_actions_of_a_run_that_did_nothing_are_said_in_full() -> None:
    assert actions(()) == NOTHING_WAS_DONE
