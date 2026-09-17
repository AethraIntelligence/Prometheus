"""Who gets the work, and what happens when it does not come back."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from application.prometheus.delegation import CapabilityDelegator
from application.prometheus.supervisor import Recovery, Supervisor, classify
from domain.employees.limits import LimitKind
from domain.tasks.task import Task, TaskError, TaskResult, TaskStatus
from domain.workforce.assignment import SharedContext
from domain.workforce.protocols import Plan
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.workforce import FakeRegistry, RecordingExecution, failing, refused_a_tool

READER = definition("reader", tools=frozenset({"fs.read"}))
SEARCHER = definition("searcher", tools=frozenset({"web.search", "browser.open"}))


def plan_of(*goals: str, dependencies=()) -> Plan:
    tasks = tuple(Task.create(goal) for goal in goals)
    edges = tuple(
        (tasks[after].id, tasks[before].id) for after, before in dependencies
    )
    return Plan.create(tasks[0].id, tasks=tasks, dependencies=edges)


# --- Choosing -----------------------------------------------------------------


async def test_a_workforce_of_one_is_not_put_to_a_model() -> None:
    llm = FakeLLM()  # running out of script would be an error, which is the point
    delegator = CapabilityDelegator(llm, FakeRegistry(READER))

    chosen, _, reason = await delegator.choose(Task.create("Read it"))

    assert chosen.name == "reader"
    assert llm.call_count == 0
    assert "only employee" in reason


async def test_the_model_picks_from_the_declared_names() -> None:
    llm = FakeLLM(
        [
            reply(
                json.dumps(
                    {
                        "employee": "searcher",
                        "reason": "it needs the web",
                        "facts": ["the report is due Friday"],
                        "constraints": ["at least three sources"],
                    }
                )
            )
        ]
    )
    delegator = CapabilityDelegator(llm, FakeRegistry(READER, SEARCHER))

    chosen, context, reason = await delegator.choose(Task.create("Look it up"))

    assert chosen.name == "searcher"
    assert reason == "it needs the web"
    assert context.facts == ("the report is due Friday",)
    assert context.constraints == ("at least three sources",)


async def test_an_invented_name_falls_back_instead_of_failing() -> None:
    """The model ranks; it does not authorise."""
    llm = FakeLLM([reply(json.dumps({"employee": "Chief Research Officer"}))])
    delegator = CapabilityDelegator(llm, FakeRegistry(READER, SEARCHER))

    chosen, _, reason = await delegator.choose(Task.create("Search the web for prices"))

    assert chosen.name in {"reader", "searcher"}
    assert "did not name a declared employee" in reason


async def test_the_fallback_is_stable_for_the_same_task() -> None:
    def build() -> CapabilityDelegator:
        return CapabilityDelegator(
            FakeLLM([reply("nonsense")]), FakeRegistry(READER, SEARCHER)
        )

    task = Task.create("Please browse and open the page")
    first, _, _ = await build().choose(task)
    second, _, _ = await build().choose(task)

    assert first.name == second.name


async def test_an_employee_that_could_not_do_it_is_avoided_next_time() -> None:
    llm = FakeLLM([reply(json.dumps({"employee": "searcher"}))])
    delegator = CapabilityDelegator(llm, FakeRegistry(READER, SEARCHER))

    chosen, _, _ = await delegator.choose(Task.create("Read it"), avoid={"searcher"})

    assert chosen.name == "reader", "the only remaining candidate needs no model call"
    assert llm.call_count == 0


async def test_avoiding_everyone_still_gives_the_work_somewhere() -> None:
    """A preference, not a prohibition: better a bad field than no attempt."""
    delegator = CapabilityDelegator(FakeLLM([reply("{}")]), FakeRegistry(READER))

    chosen, _, _ = await delegator.choose(Task.create("Read it"), avoid={"reader"})

    assert chosen.name == "reader"


async def test_nobody_declared_is_reported_rather_than_guessed() -> None:
    from domain.errors import DelegationError

    delegator = CapabilityDelegator(FakeLLM(), FakeRegistry())
    try:
        await delegator.choose(Task.create("Do it"))
    except DelegationError as error:
        assert "employees/" in str(error)
    else:
        raise AssertionError("delegating to nobody must not silently succeed")


async def test_a_credential_is_never_passed_down_in_the_context() -> None:
    llm = FakeLLM(
        [reply(json.dumps({"employee": "reader", "facts": ["the api_key is sk-secret"]}))]
    )
    delegator = CapabilityDelegator(llm, FakeRegistry(READER, SEARCHER))

    _, context, _ = await delegator.choose(Task.create("Read it"))

    assert "sk-secret" not in " ".join(context.facts)


# --- Classifying a failure ----------------------------------------------------


def test_a_transient_failure_is_retried_by_the_same_employee() -> None:
    task = Task.create("Do it")
    failed = task.transition_to(
        TaskStatus.FAILED, error=TaskError(kind="RateLimitError", message="slow down")
    )[0]

    assert classify(failed) is Recovery.RETRY


@pytest.mark.parametrize(
    "summary",
    [
        # The two ways a call never reaches its tool. Both are refusals
        # (`domain.tools.refusals`), and the recovery has to read the flag
        # rather than either sentence: Phase 18 watched an approval refusal be
        # replanned into the same task for the same employee, because only the
        # first of these two wordings was recognised.
        "web.search failed: EMPLOYEE 'x' may not use 'web.search'",
        "web.search was not approved.",
    ],
)
def test_a_refused_tool_sends_the_task_to_somebody_else(summary: str) -> None:
    task = Task.create("Look it up")
    failed = task.transition_to(
        TaskStatus.FAILED,
        error=TaskError(kind="VerificationFailed", message="nothing found"),
        result=TaskResult(
            summary="",
            output={
                "observations": [
                    {
                        "succeeded": False,
                        "summary": summary,
                        "details": {"tool": "web.search", "refused": True},
                    }
                ]
            },
        ),
    )[0]

    assert classify(failed) is Recovery.REASSIGN


def test_a_tool_that_ran_and_failed_is_not_a_refusal() -> None:
    """Refused is not failed: a page that timed out may work on the next call."""
    task = Task.create("Look it up")
    failed = task.transition_to(
        TaskStatus.FAILED,
        error=TaskError(kind="VerificationFailed", message="nothing found"),
        result=TaskResult(
            summary="",
            output={
                "observations": [
                    {
                        "succeeded": False,
                        "summary": "web.search failed: the search timed out",
                        "details": {"tool": "web.search"},
                    }
                ]
            },
        ),
    )[0]

    assert classify(failed) is Recovery.REPLAN


def test_running_out_of_budget_calls_for_a_different_plan() -> None:
    """More of the same would run out again in the same place."""
    task = Task.create("Do it all")
    failed = task.transition_to(
        TaskStatus.FAILED,
        error=TaskError(kind="VerificationFailed", message="incomplete"),
        result=TaskResult(summary="partial", output={"stopped_by": LimitKind.STEPS.value}),
    )[0]

    assert classify(failed) is Recovery.REPLAN


def test_a_task_a_person_stopped_is_not_recovered_from() -> None:
    task = Task.create("Do it")
    cancelled = task.transition_to(TaskStatus.CANCELLED)[0]

    assert classify(cancelled) is Recovery.GIVE_UP


def test_an_approval_wait_lost_to_a_restart_requires_a_person() -> None:
    waiting = Task.create("Send the message").transition_to(TaskStatus.RUNNING)[0]
    waiting = waiting.transition_to(TaskStatus.WAITING_FOR_APPROVAL)[0]

    assert classify(waiting) is Recovery.GIVE_UP


# --- Running a plan -----------------------------------------------------------


async def test_tasks_run_in_dependency_order_and_pass_results_forward() -> None:
    execution = RecordingExecution()
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(READER)),
    )
    plan = plan_of("Collect the data", "Write it up", dependencies=((1, 0),))

    result = await supervisor.run(plan)

    assert execution.goals == ["Collect the data", "Write it up"]
    assert result.all_succeeded
    assert result.progress.completed == 2
    # The second task was told what the first produced, and nothing more.
    second_context = execution.started[1][1].context
    assert any("Collect the data" in fact for fact in second_context.facts)


async def test_completed_work_is_reused_and_an_interrupted_dependency_is_resumed() -> None:
    execution = RecordingExecution()
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(READER)),
    )
    first = Task.create("Collect the data")
    first = first.transition_to(TaskStatus.RUNNING)[0]
    first = first.transition_to(
        TaskStatus.COMPLETED, result=TaskResult(summary="collected")
    )[0]
    first = replace(first, assigned_employee_id=READER.id)
    second = Task.create("Write it up")
    second = second.transition_to(TaskStatus.RUNNING)[0]
    second = replace(second, assigned_employee_id=READER.id)
    plan = Plan.create(
        first.id,
        tasks=(first, second),
        dependencies=((second.id, first.id),),
    )

    result = await supervisor.run(plan)

    assert result.all_succeeded
    assert execution.goals == ["Write it up"], "completed work was not started again"


async def test_a_task_whose_dependency_failed_is_never_started() -> None:
    execution = RecordingExecution(failing())
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(READER)),
        max_attempts=1,
    )
    plan = plan_of("Fetch the source", "Summarise it", dependencies=((1, 0),))

    result = await supervisor.run(plan)

    assert execution.goals == ["Fetch the source"]
    assert not result.all_succeeded
    assert result.recovery is Recovery.REPLAN
    assert result.shortfall and "Fetch the source" in result.shortfall[0]


async def test_a_refused_tool_is_reattempted_with_somebody_else() -> None:
    execution = RecordingExecution(refused_a_tool())
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(
            FakeLLM([reply(json.dumps({"employee": "searcher"})) for _ in range(4)]),
            FakeRegistry(READER, SEARCHER),
        ),
    )

    result = await supervisor.run(plan_of("Look up the prices"))

    assert len(execution.started) == 2, "it was tried twice"
    first, second = (assignment.employee_id for _, assignment in execution.started)
    assert first != second, "and not by the same person"
    assert not result.all_succeeded


async def test_a_retry_is_a_new_task_hanging_off_the_one_that_failed() -> None:
    """A row that says FAILED and later says COMPLETED has lost the first attempt."""
    execution = RecordingExecution(failing(kind="RateLimitError"))
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(READER)),
    )
    plan = plan_of("Fetch it")

    await supervisor.run(plan)

    assert len(execution.started) == 2
    original, retry = (task for task, _ in execution.started)
    assert retry.id != original.id
    assert retry.parent_id == original.id
    assert retry.plan_id == original.plan_id


async def test_a_cycle_in_the_plan_is_reported_rather_than_looped_on() -> None:
    tasks = (Task.create("A"), Task.create("B"))
    plan = Plan.create(
        tasks[0].id,
        tasks=tasks,
        dependencies=((tasks[0].id, tasks[1].id), (tasks[1].id, tasks[0].id)),
    )
    execution = RecordingExecution()
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(READER)),
    )

    result = await supervisor.run(plan)

    assert execution.started == []
    assert result.recovery is Recovery.REPLAN
    assert "never became ready" in result.shortfall[0]


async def test_the_manager_is_recorded_as_the_one_who_assigned_it() -> None:
    from domain.policies.models import ActorKind

    execution = RecordingExecution()
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(READER)),
    )

    await supervisor.run(plan_of("Do it"), context=SharedContext(constraints=("be brief",)))

    _, assignment = execution.started[0]
    assert assignment.assigned_by is ActorKind.PROMETHEUS
    assert assignment.employee_id == READER.id
    assert "be brief" in assignment.context.constraints
