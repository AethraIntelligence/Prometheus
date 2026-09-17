"""Predefined processes (Phase 10, §10.7-10.8).

The engine decides nothing about how work is done, so what is worth testing is
the graph, the retries and the failure rule - and that it reaches the outside
world only through `StepExecution`, which is what keeps there being one runtime.
"""

from __future__ import annotations

import pytest

from application.workflows.engine import WorkflowEngine, order_steps
from domain.errors import ConfigurationError, NotFoundError
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workflows.definition import (
    OnFailure,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowTrigger,
)
from domain.workflows.run import RunStatus
from infrastructure.persistence.workflow_repository import InMemoryWorkflowRunRepository


class Registry:
    """Implements `domain.workflows.protocols.WorkflowRegistry`."""

    def __init__(self, *definitions: WorkflowDefinition) -> None:
        self._by_name = {definition.name: definition for definition in definitions}

    def list_all(self):
        return list(self._by_name.values())

    def get(self, name: str) -> WorkflowDefinition:
        if name not in self._by_name:
            raise NotFoundError(f"Unknown workflow: {name}")
        return self._by_name[name]


class Runner:
    """Implements `domain.workflows.protocols.StepExecution`, with no model."""

    def __init__(self, *, failing: set[str] | None = None, raising: set[str] | None = None) -> None:
        self.goals: list[tuple[str, str]] = []
        self.directions: list[tuple[str, str]] = []
        self._failing = failing or set()
        self._raising = raising or set()

    async def submit_and_run(self, goal: str, employee_name: str, **kwargs: object) -> Task:
        from domain.workforce import directions

        self.goals.append((employee_name, goal))
        current = directions.current()
        self.directions.append((current.approvals.value, current.model))
        if employee_name in self._raising:
            raise RuntimeError(f"{employee_name} is not declared here")
        task = Task.create(goal)
        if employee_name in self._failing:
            return task.transition_to(TaskStatus.FAILED)[0]
        running = task.transition_to(TaskStatus.RUNNING)[0]
        verifying = running.transition_to(TaskStatus.VERIFYING)[0]
        return verifying.complete(TaskResult(summary=f"{employee_name} did it"))[0]


def step(name: str, employee: str = "worker", **extra) -> WorkflowStep:
    return WorkflowStep(
        name=name, employee=employee, instruction=extra.pop("instruction", name), **extra
    )


def flow(*steps: WorkflowStep, name: str = "flow", **extra) -> WorkflowDefinition:
    return WorkflowDefinition(name=name, steps=steps, **extra)


def engine(definition: WorkflowDefinition, runner: Runner) -> tuple[WorkflowEngine, object]:
    runs = InMemoryWorkflowRunRepository()
    return WorkflowEngine(Registry(definition), runner, runs), runs


# --- The graph ----------------------------------------------------------------


def test_steps_with_no_dependencies_run_in_the_order_they_are_written():
    """A workflow that declares nothing about order should run as it reads."""
    ordered = order_steps((step("a"), step("b"), step("c")))
    assert [s.name for s in ordered] == ["a", "b", "c"]


def test_a_dependency_moves_a_step_after_what_it_needs():
    ordered = order_steps((step("last", depends_on=("first",)), step("first")))
    assert [s.name for s in ordered] == ["first", "last"]


def test_a_cycle_is_reported_rather_than_looped_on():
    with pytest.raises(ConfigurationError, match="cycle"):
        order_steps((step("a", depends_on=("b",)), step("b", depends_on=("a",))))


def test_a_dependency_on_a_step_that_does_not_exist_names_it():
    with pytest.raises(ConfigurationError, match="renamed"):
        order_steps((step("a", depends_on=("renamed",)),))


def test_two_steps_with_one_name_are_refused():
    with pytest.raises(ConfigurationError, match="Duplicate"):
        order_steps((step("a"), step("a")))


# --- Running ------------------------------------------------------------------


async def test_a_run_records_every_step_and_completes():
    runner = Runner()
    engine_, runs = engine(flow(step("one"), step("two", depends_on=("one",))), runner)

    run = await engine_.run("flow")

    assert run.status is RunStatus.COMPLETED
    assert [outcome.step for outcome in run.steps] == ["one", "two"]
    assert await runs.get(run.id) is not None


async def test_an_input_is_substituted_into_the_instruction():
    runner = Runner()
    definition = flow(
        step("one", instruction="Sort {folder}"), inputs={"folder": "sales"}, name="flow"
    )
    engine_, _ = engine(definition, runner)

    await engine_.run("flow")

    assert runner.goals == [("worker", "Sort sales")]


async def test_a_later_step_can_read_what_an_earlier_one_produced():
    runner = Runner()
    definition = flow(
        step("first"),
        step("second", instruction="Given {steps.first}, continue", depends_on=("first",)),
    )
    engine_, _ = engine(definition, runner)

    await engine_.run("flow")

    assert runner.goals[1][1] == "Given worker did it, continue"


async def test_an_input_nobody_supplied_is_left_alone_rather_than_crashing_the_run():
    """Half an instruction is worse than a literal brace: the model can say it
    does not understand, a crash mid-run cannot."""
    runner = Runner()
    engine_, _ = engine(flow(step("one", instruction="Sort {nothing}")), runner)

    run = await engine_.run("flow")

    assert run.status is RunStatus.COMPLETED
    assert runner.goals == [("worker", "Sort {nothing}")]


# --- Failure and retry (§10.8) ------------------------------------------------


async def test_a_failed_step_stops_the_run_by_default():
    """The step after it usually reads what it produced."""
    runner = Runner(failing={"broken"})
    engine_, _ = engine(
        flow(step("one", employee="broken"), step("two", depends_on=("one",))), runner
    )

    run = await engine_.run("flow")

    assert run.status is RunStatus.FAILED
    assert [outcome.step for outcome in run.steps] == ["one"]
    assert len(runner.goals) == 1


async def test_a_step_declared_survivable_lets_the_run_continue():
    runner = Runner(failing={"broken"})
    engine_, _ = engine(
        flow(step("one", employee="broken", on_failure=OnFailure.CONTINUE), step("two")), runner
    )

    run = await engine_.run("flow")

    assert run.status is RunStatus.COMPLETED
    assert [outcome.succeeded for outcome in run.steps] == [False, True]


async def test_a_step_is_retried_as_many_times_as_it_declared():
    runner = Runner(failing={"broken"})
    engine_, _ = engine(flow(step("one", employee="broken", max_attempts=3)), runner)

    run = await engine_.run("flow")

    assert len(runner.goals) == 3
    assert run.steps[0].attempts == 3


async def test_a_step_that_succeeds_is_not_retried():
    runner = Runner()
    engine_, _ = engine(flow(step("one", max_attempts=3)), runner)

    run = await engine_.run("flow")

    assert len(runner.goals) == 1
    assert run.steps[0].attempts == 1


async def test_a_step_that_cannot_even_start_is_a_failed_step_not_a_crashed_run():
    """An employee that is not declared here, a provider with no key. The rest
    of the workflow still gets a verdict."""
    runner = Runner(raising={"missing"})
    engine_, _ = engine(flow(step("one", employee="missing")), runner)

    run = await engine_.run("flow")

    assert run.status is RunStatus.FAILED
    assert run.steps[0].succeeded is False


async def test_the_run_is_written_down_before_the_first_step():
    """A killed process must leave a run to account for, not orphan tasks."""
    saved: list[str] = []

    class Recording(InMemoryWorkflowRunRepository):
        async def save(self, run):
            saved.append(run.status.value)
            await super().save(run)

    runs = Recording()
    engine_ = WorkflowEngine(Registry(flow(step("one"))), Runner(), runs)

    await engine_.run("flow")

    assert saved[0] == RunStatus.RUNNING.value


async def test_the_trigger_is_recorded_on_the_run():
    engine_, _ = engine(flow(step("one")), Runner())
    run = await engine_.run("flow", trigger=WorkflowTrigger.MANUAL)
    assert run.trigger is WorkflowTrigger.MANUAL


async def test_a_run_and_dry_run_use_the_same_typed_version() -> None:
    from domain.workflows.definition import InputKind, WorkflowInput

    runner = Runner()
    definition = flow(
        step("one", instruction="Make {count} reports"),
        version=3,
        input_schema={"count": WorkflowInput(InputKind.INTEGER, required=True)},
    )
    engine_, _ = engine(definition, runner)

    preview = engine_.dry_run("flow", inputs={"count": 2})
    run = await engine_.run("flow", inputs={"count": 2})

    assert preview["version"] == 3
    assert preview["steps"][0]["instruction"] == "Make 2 reports"
    assert run.workflow_version == 3
    assert runner.goals == [("worker", "Make 2 reports")]


async def test_a_workflow_budget_stops_later_work() -> None:
    from domain.workflows.definition import WorkflowBudget

    runner = Runner()
    engine_, _ = engine(
        flow(step("one", max_attempts=2), budget=WorkflowBudget(max_steps=1)),
        runner,
    )

    with pytest.raises(ConfigurationError, match="above its budget"):
        await engine_.run("flow")


async def test_the_workflow_profile_reaches_every_step() -> None:
    from domain.workflows.definition import WorkflowProfile
    from domain.workforce.directions import ApprovalChoice

    runner = Runner()
    engine_, _ = engine(
        flow(
            step("one"),
            profile=WorkflowProfile(approvals=ApprovalChoice.DENY, model="careful"),
        ),
        runner,
    )

    await engine_.run("flow")

    assert runner.directions == [("DENY", "careful")]
