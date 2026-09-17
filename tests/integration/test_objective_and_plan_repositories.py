"""The manager's executable record survives a restart on both implementations."""

from __future__ import annotations

from uuid import uuid4

import pytest

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.tasks.task import Task, TaskCreatedBy, TaskStatus
from domain.workforce.protocols import (
    Objective,
    ObjectiveResult,
    ObjectiveStatus,
    Plan,
    PlanStatus,
)
from domain.workforce.routing import Requirement
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
from infrastructure.persistence.objective_repository import (
    InMemoryObjectiveRepository,
    SqlObjectiveRepository,
)
from infrastructure.persistence.plan_repository import (
    InMemoryPlanRepository,
    SqlPlanRepository,
)
from infrastructure.persistence.task_repository import SqlTaskRepository


@pytest.fixture(params=["in_memory", "sqlite"])
def store(request: pytest.FixtureRequest):
    """The three repositories together, and always the same implementation.

    They reference each other - a plan's rows point at an objective and at
    tasks - so mixing an in-memory objective store with a SQLite plan store
    would be testing a configuration nothing builds.
    """
    if request.param == "in_memory":
        tasks = InMemoryTaskRepository()
        return InMemoryObjectiveRepository(), InMemoryPlanRepository(tasks), tasks
    factory = request.getfixturevalue("session_factory")
    return (
        SqlObjectiveRepository(factory),
        SqlPlanRepository(factory),
        SqlTaskRepository(factory),
    )


@pytest.fixture
def objectives(store):
    return store[0]


@pytest.fixture
def plans(store):
    return store[1], store[2]


# --- Objectives ---------------------------------------------------------------


async def test_an_objective_round_trips_with_what_prometheus_read_into_it(objectives) -> None:
    objective = Objective.create(
        "Find twenty things",
        constraints={"count": 20},
        acceptance_criteria=("at least 20 rows",),
    )
    await objectives.save(objective)

    loaded = await objectives.get(objective.id)
    assert loaded is not None
    assert loaded.text == "Find twenty things"
    assert loaded.constraints == {"count": 20}
    assert loaded.acceptance_criteria == ("at least 20 rows",)
    assert loaded.status is ObjectiveStatus.RECEIVED
    assert loaded.finished_at is None


async def test_the_answer_and_what_is_missing_are_both_stored(objectives) -> None:
    objective = Objective.create("Find twenty things")
    result = ObjectiveResult(
        objective_id=objective.id,
        summary="Here are eleven.",
        status=ObjectiveStatus.ESCALATED,
        output={"completed": 1},
        missing=("nine more",),
        cost_usd=0.25,
    )
    await objectives.save(objective.to(ObjectiveStatus.ESCALATED, result))

    loaded = await objectives.get(objective.id)
    assert loaded is not None and loaded.result is not None
    assert loaded.result.summary == "Here are eleven."
    assert loaded.result.missing == ("nine more",)
    assert loaded.result.cost_usd == 0.25
    assert loaded.is_terminal
    assert loaded.finished_at is not None


async def test_recent_objectives_are_newest_first_and_scoped(objectives) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    ids = []
    for index in range(3):
        objective = replace(
            Objective.create(f"Objective {index}"), created_at=now + timedelta(seconds=index)
        )
        await objectives.save(objective)
        ids.append(objective.id)
    elsewhere = replace(Objective.create("Not mine"), workspace_id=WorkspaceId(uuid4()))
    await objectives.save(elsewhere)

    recent = await objectives.list_recent()
    assert [item.id for item in recent] == list(reversed(ids))
    assert len(await objectives.list_recent(limit=2)) == 2


async def test_incomplete_objectives_are_recovered_across_workspaces(objectives) -> None:
    first = Objective.create("First")
    second = Objective.create("Second", workspace_id=WorkspaceId(uuid4()))
    done = Objective.create("Done")
    done = done.to(
        ObjectiveStatus.DONE,
        ObjectiveResult(objective_id=done.id, summary="done", status=ObjectiveStatus.DONE),
    )
    for objective in (second, done, first):
        await objectives.save(objective)

    recovered = await objectives.list_incomplete()

    assert {item.id for item in recovered} == {first.id, second.id}


# --- Plans --------------------------------------------------------------------


async def test_a_plan_round_trips_with_its_tasks_and_edges(plans, objectives) -> None:
    plan_repository, task_repository = plans
    objective = Objective.create("Do two things")
    await objectives.save(objective)

    first, second = (
        _planned("Collect the data", objective, plan_id := uuid4(), priority=5),
        _planned("Write it up", objective, plan_id, priority=4),
    )
    for task in (first, second):
        await task_repository.save(task)

    plan = Plan(
        id=plan_id,
        objective_id=objective.id,
        tasks=(first, second),
        dependencies=((second.id, first.id),),
        rationale="Fetch, then write.",
        status=PlanStatus.RUNNING,
    )
    await plan_repository.save(plan)

    loaded = await plan_repository.get(plan.id)
    assert loaded is not None
    assert [task.goal for task in loaded.tasks] == ["Collect the data", "Write it up"]
    assert loaded.dependencies == ((second.id, first.id),)
    assert loaded.depends_on(second.id) == frozenset({first.id})
    assert loaded.ready(set()) == (loaded.tasks[0],), "the second waits on the first"
    assert loaded.status is PlanStatus.RUNNING
    assert loaded.rationale == "Fetch, then write."


async def test_an_unstarted_plan_survives_with_tasks_and_routing_needs(
    plans, objectives
) -> None:
    """A crash may land after planning and before the first delegation."""
    plan_repository, task_repository = plans
    objective = Objective.create("Research and report")
    await objectives.save(objective)
    plan_id = uuid4()
    first = _planned("Research", objective, plan_id, priority=5)
    second = _planned("Report", objective, plan_id, priority=4)
    needed = Requirement(
        capabilities=CapabilityRequirement(required=frozenset({Capability.WEB_BROWSING})),
        services=frozenset({"notes"}),
    )
    plan = Plan(
        id=plan_id,
        objective_id=objective.id,
        tasks=(first, second),
        dependencies=((second.id, first.id),),
        requirements={first.id: needed},
        status=PlanStatus.RUNNING,
    )

    await plan_repository.save(plan)

    assert await task_repository.get(first.id) is None, "planning is not execution"
    recovered = await plan_repository.get(plan.id)
    assert recovered is not None
    assert [task.id for task in recovered.tasks] == [first.id, second.id]
    assert recovered.dependencies == ((second.id, first.id),)
    assert recovered.requirements[first.id] == needed


async def test_a_task_status_is_read_back_from_the_tasks_table(plans, objectives) -> None:
    """The plan holds no copy of it, so it cannot go stale."""
    plan_repository, task_repository = plans
    objective = Objective.create("Do it")
    await objectives.save(objective)
    plan_id = uuid4()
    task = _planned("Do it", objective, plan_id)
    await task_repository.save(task)
    await plan_repository.save(Plan(id=plan_id, objective_id=objective.id, tasks=(task,)))

    finished, event = task.transition_to(TaskStatus.RUNNING)
    await task_repository.save(finished, event)

    loaded = await plan_repository.get(plan_id)
    assert loaded is not None
    assert loaded.tasks[0].status is TaskStatus.RUNNING


async def test_revisions_are_kept_and_returned_newest_first(plans, objectives) -> None:
    plan_repository, task_repository = plans
    objective = Objective.create("Try twice")
    await objectives.save(objective)

    for revision in (1, 2):
        plan_id = uuid4()
        task = _planned(f"Attempt {revision}", objective, plan_id)
        await task_repository.save(task)
        await plan_repository.save(
            Plan(
                id=plan_id,
                objective_id=objective.id,
                tasks=(task,),
                revision=revision,
                status=PlanStatus.SUPERSEDED if revision == 1 else PlanStatus.DONE,
            )
        )

    revisions = await plan_repository.for_objective(objective.id)
    assert [p.revision for p in revisions] == [2, 1]
    assert revisions[1].status is PlanStatus.SUPERSEDED, "the first attempt is not deleted"


async def test_saving_a_plan_twice_leaves_one_set_of_edges(plans, objectives) -> None:
    plan_repository, task_repository = plans
    objective = Objective.create("Do it")
    await objectives.save(objective)
    plan_id = uuid4()
    first = _planned("A", objective, plan_id, priority=5)
    second = _planned("B", objective, plan_id, priority=4)
    for task in (first, second):
        await task_repository.save(task)

    plan = Plan(
        id=plan_id,
        objective_id=objective.id,
        tasks=(first, second),
        dependencies=((second.id, first.id),),
    )
    await plan_repository.save(plan)
    await plan_repository.save(plan.to(PlanStatus.RUNNING))

    loaded = await plan_repository.get(plan_id)
    assert loaded is not None
    assert loaded.dependencies == ((second.id, first.id),)


def _planned(goal: str, objective: Objective, plan_id, *, priority: int = 5) -> Task:
    from dataclasses import replace

    return replace(
        Task.create(
            goal,
            workspace_id=objective.workspace_id,
            created_by=TaskCreatedBy.PROMETHEUS,
            priority=priority,
        ),
        plan_id=plan_id,
    )


async def test_a_plan_holds_its_edges_before_any_of_its_tasks_have_started(
    plans, objectives
) -> None:
    """Planning is durable before delegation creates live task rows."""
    plan_repository, _ = plans
    objective = Objective.create("Do two things")
    await objectives.save(objective)
    plan_id = uuid4()
    first = _planned("A", objective, plan_id)
    second = _planned("B", objective, plan_id)

    await plan_repository.save(
        Plan(
            id=plan_id,
            objective_id=objective.id,
            tasks=(first, second),
            dependencies=((second.id, first.id),),
        )
    )

    loaded = await plan_repository.get(plan_id)
    assert loaded is not None
    assert [task.id for task in loaded.tasks] == [first.id, second.id]
    assert all(task.status is TaskStatus.CREATED for task in loaded.tasks)
    assert loaded.dependencies == ((second.id, first.id),), "the shape is recorded"
