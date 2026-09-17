from __future__ import annotations

from pathlib import Path

import pytest

from application.employee_runtime.executor import Executor
from application.employee_runtime.planner import Planner
from application.employee_runtime.runtime import EmployeeRuntime, RuntimeDependencies
from application.employee_runtime.verifier import Verifier
from application.task_runner import TaskRunner
from domain.errors import ProviderUnavailableError
from domain.policies.models import ActorKind
from domain.tasks.task import TaskStatus
from domain.workforce.assignment import AssignmentOutcome, SharedContext
from infrastructure.employees.yaml_registry import YamlEmployeeRegistry
from infrastructure.persistence.assignment_repository import InMemoryAssignmentRepository
from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
from infrastructure.tools.registry import InMemoryToolRegistry
from tests.fakes.llm import FakeLLM, reply

PLAN = reply('{"steps": [{"description": "Answer it"}]}')
PASS = reply('{"passed": true, "reason": "fine"}')

EMPLOYEE_YAML = "name: researcher\nrole: Research Specialist\n"


@pytest.fixture
def registry(tmp_path: Path) -> YamlEmployeeRegistry:
    (tmp_path / "researcher").mkdir()
    (tmp_path / "researcher" / "employee.yaml").write_text(EMPLOYEE_YAML, encoding="utf-8")
    return YamlEmployeeRegistry(tmp_path)


def build(registry, script, **kwargs):
    llm = FakeLLM(script)
    tasks = InMemoryTaskRepository()
    assignments = InMemoryAssignmentRepository()
    tools = InMemoryToolRegistry()

    async def build_runtime(definition):
        return EmployeeRuntime(
            definition,
            RuntimeDependencies(
                planner=Planner(llm),
                executor=Executor(llm, tools),
                verifier=Verifier(llm),
                tasks=tasks,
                tools=tools,
                limits=definition.limits,
            ),
        )

    runner = TaskRunner(
        tasks=tasks,
        assignments=assignments,
        registry=registry,
        build_runtime=build_runtime,
        **kwargs,
    )
    return runner, tasks, assignments, llm


async def test_the_task_and_who_it_went_to_are_recorded_before_any_work(registry) -> None:
    # Written down first, so a process killed one second later still has a task
    # to come back to.
    runner, tasks, _assignments, llm = build(registry, [])

    task, assignment = await runner.submit("Explain WAL mode", "researcher")

    assert (await tasks.get(task.id)).status is TaskStatus.CREATED
    assert assignment.task_id == task.id
    assert assignment.assigned_by is ActorKind.USER
    assert assignment.accepted_at is not None
    assert llm.call_count == 0


async def test_a_submitted_task_names_its_employee(registry) -> None:
    runner, _, _, _ = build(registry, [])
    task, _ = await runner.submit("Explain WAL mode", "researcher")

    assert task.assigned_employee_id == registry.get("researcher").id


async def test_running_a_task_closes_its_assignment(registry) -> None:
    runner, _tasks, assignments, _ = build(registry, [PLAN, reply("An answer."), PASS])

    task = await runner.submit_and_run("Explain WAL mode", "researcher")

    assert task.status is TaskStatus.COMPLETED
    closed = (await assignments.for_task(task.id))[0]
    assert closed.outcome is AssignmentOutcome.COMPLETED
    assert closed.completed_at is not None
    assert closed.result.summary == "An answer."


async def test_a_failed_task_closes_its_assignment_as_failed(registry) -> None:
    reject = reply('{"passed": false, "reason": "no"}')
    runner, _, assignments, _ = build(
        registry, [PLAN, reply("thin"), reject, PLAN, reply("still thin"), reject]
    )

    task = await runner.submit_and_run("Explain WAL mode", "researcher")

    assert task.status is TaskStatus.FAILED
    assert (await assignments.for_task(task.id))[0].outcome is AssignmentOutcome.FAILED


async def test_context_handed_down_is_stored_with_the_assignment(registry) -> None:
    runner, _, assignments, _ = build(registry, [])
    context = SharedContext(facts=("the deadline is Friday",), constraints=("no phone calls",))

    _, assignment = await runner.submit(
        "Explain WAL mode", "researcher", context=context
    )

    stored = await assignments.get(assignment.id)
    assert stored.context.facts == ("the deadline is Friday",)
    assert stored.context.constraints == ("no phone calls",)


async def test_a_transient_failure_is_retried_at_the_task_level(registry) -> None:
    runner, _, _, llm = build(
        registry,
        [PLAN, ProviderUnavailableError("down"), reply("An answer."), PASS],
    )

    task = await runner.submit_and_run("Explain WAL mode", "researcher")

    assert task.status is TaskStatus.COMPLETED
    # The second attempt resumed into execution rather than planning again: the
    # plan survived the failure, so paying for another one would be waste.
    assert llm.call_count == 4


async def test_a_permanent_failure_is_not_retried_and_is_written_down(registry) -> None:
    from domain.errors import InvalidRequestError

    runner, _tasks, _, llm = build(
        registry, [PLAN, InvalidRequestError("malformed tool schema")]
    )

    task = await runner.submit_and_run("Explain WAL mode", "researcher")

    assert task.status is TaskStatus.FAILED
    assert task.error.kind == "InvalidRequestError"
    assert task.error.details["kind"] == "PERMANENT"
    assert llm.call_count == 2, "no second attempt"


async def test_retries_stop_at_the_configured_limit(registry) -> None:
    runner, _, _, llm = build(
        registry,
        [PLAN, *[ProviderUnavailableError("down")] * 5],
        max_attempts=3,
    )

    task = await runner.submit_and_run("Explain WAL mode", "researcher")

    assert task.status is TaskStatus.FAILED
    assert task.error.details["kind"] == "TRANSIENT"
    # Planned once, then three attempts at the work itself.
    assert llm.call_count == 4


async def test_a_task_that_fails_before_it_starts_is_still_recorded(registry) -> None:
    # Its employee was removed between submitting and running. Burying that
    # behind an illegal-transition error would hide the real cause.
    from domain.errors import EmployeeNotFoundError

    runner, _tasks, _, _ = build(registry, [])
    task, _ = await runner.submit("Explain WAL mode", "researcher")

    async def missing(_definition):
        raise EmployeeNotFoundError("researcher")

    runner._build_runtime = missing

    final = await runner.run(task)
    assert final.status is TaskStatus.FAILED
    assert final.error.kind == "EmployeeNotFoundError"


async def test_only_unfinished_work_is_offered_for_resuming(registry) -> None:
    runner, _, _, _ = build(registry, [PLAN, reply("An answer."), PASS])

    done = await runner.submit_and_run("Explain WAL mode", "researcher")
    pending, _ = await runner.submit("Something else", "researcher")

    resumable = [task.id for task in await runner.resumable()]
    assert pending.id in resumable
    assert done.id not in resumable


async def test_resuming_finds_the_assignment_the_task_already_had(registry) -> None:
    runner, tasks, assignments, _ = build(registry, [PLAN, reply("An answer."), PASS])
    task, assignment = await runner.submit("Explain WAL mode", "researcher")

    resumed = await runner.resume(await tasks.get(task.id))

    assert resumed.status is TaskStatus.COMPLETED
    assert (await assignments.get(assignment.id)).outcome is AssignmentOutcome.COMPLETED


async def test_a_task_runs_under_its_escalation_and_bills_its_own_calls(registry) -> None:
    """Phase 10: the manager's escalation reaches every model call of the run.

    Read off the stored assignment rather than passed along a call chain, so a
    resumed escalation is still one, and every call is attributed to the task
    that made it - which is what lets the trace say why each model ran.
    """
    from domain.llm import escalation, routing

    seen: list[tuple[object, object]] = []

    def watch(request):
        del request
        seen.append((escalation.current().to_dict(), routing.billed_task()))

    runner, _, _, llm = build(registry, [PLAN, reply("An answer."), PASS])
    llm._on_request = watch
    context = SharedContext(data={"escalation": {"level": 1, "cause": "NOT_ACCEPTED"}})

    task, assignment = await runner.submit("Explain WAL mode", "researcher", context=context)
    finished = await runner.run(task, assignment)

    assert finished.status is TaskStatus.COMPLETED
    assert seen and all(
        state == {"level": 1, "cause": "NOT_ACCEPTED"} and billed == task.id
        for state, billed in seen
    )
    assert escalation.current().level == 0, "the escalation belongs to that run only"


# --- Phase 11: what an assignment records ---------------------------------------


async def test_a_run_by_name_records_that_a_person_chose_and_the_verdict_on_the_evidence(
    registry,
) -> None:
    from domain.workforce.decision import SelectionCode

    runner, _, assignments, _ = build(registry, [PLAN, reply("An answer."), PASS])

    task = await runner.submit_and_run("Explain WAL mode", "researcher")

    [closed] = await assignments.for_task(task.id)
    assert closed.decision.code is SelectionCode.CHOSEN_BY_PERSON
    # No contract, no tools: judgement is real work, and it is accepted.
    assert closed.acceptance is not None and closed.acceptance.accepted


async def test_a_cancelled_task_is_not_recorded_as_the_employees_failure(registry) -> None:
    from domain.tasks.task import TaskStatus as Status

    runner, tasks, assignments, _ = build(registry, [])
    task, assignment = await runner.submit("Explain WAL mode", "researcher")
    cancelled, event = task.transition_to(Status.CANCELLED)
    await tasks.save(cancelled, event)

    await runner._close(assignment, cancelled)

    [closed] = await assignments.for_task(task.id)
    assert closed.outcome is AssignmentOutcome.CANCELLED
    assert closed.acceptance is None, "nothing completed, so nothing was judged"


async def test_a_task_that_lost_its_assignment_to_a_crash_gets_exactly_one_on_resume(
    registry,
) -> None:
    from dataclasses import replace as replaced

    from domain.tasks.task import Task
    from domain.workforce.decision import SelectionCode

    runner, tasks, assignments, _ = build(
        registry, [PLAN, reply("An answer."), PASS]
    )
    # Written with its employee, and the process stopped before the assignment was.
    orphan = replaced(
        Task.create("Explain WAL mode"), assigned_employee_id=registry.get("researcher").id
    )
    await tasks.save(orphan)

    finished = await runner.resume(orphan)

    assert finished.status is TaskStatus.COMPLETED
    [recovered] = await assignments.for_task(orphan.id)
    assert recovered.decision.code is SelectionCode.UNRECORDED
    assert recovered.outcome is AssignmentOutcome.COMPLETED


async def test_a_task_that_died_waiting_on_a_person_is_put_back_where_resume_reaches_it() -> None:
    """Phase 13: after a crash nobody can answer the question, and nothing had happened yet."""
    from domain.tasks.task import Task

    tasks = InMemoryTaskRepository()
    task = Task.create("Overwrite the report")
    running, event = task.transition_to(TaskStatus.RUNNING)
    await tasks.save(running, event)
    waiting, event = running.transition_to(TaskStatus.WAITING_FOR_APPROVAL)
    await tasks.save(waiting, event)
    runner = TaskRunner(
        tasks=tasks,
        assignments=InMemoryAssignmentRepository(),
        registry=None,  # type: ignore[arg-type]
        build_runtime=None,  # type: ignore[arg-type]
    )
    assert await runner.resumable() == [], "stuck: neither finished nor resumable"

    assert await runner.reconcile_abandoned_approvals() == 1

    [resumable] = await runner.resumable()
    assert resumable.id == task.id and resumable.status is TaskStatus.RUNNING
    [*_, last] = await tasks.events(task.id)
    assert "expired" in last.payload["reason"]
