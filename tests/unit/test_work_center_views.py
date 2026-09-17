from __future__ import annotations

from dataclasses import replace

from application.interface import views
from domain.tasks.task import Task, TaskError, TaskStatus
from domain.workforce.protocols import Objective, ObjectiveStatus, Plan, PlanStatus


def test_active_work_explains_the_next_action_and_execution_owner() -> None:
    objective = replace(
        Objective.create("Prepare a launch report"),
        status=ObjectiveStatus.RUNNING,
        acceptance_criteria=("Covers revenue and risks",),
    )
    task = replace(
        Task.create("Collect reliable figures"),
        status=TaskStatus.RUNNING,
        assignment_reason="best match for source research",
    )
    plan = replace(
        Plan.create(objective.id, tasks=(task,)),
        status=PlanStatus.RUNNING,
        rationale="Research before writing.",
    )
    task_view = views.work_task(task, employee=None, calls=[], model_calls=[])

    item = views.work_item(
        objective,
        plans=[plan],
        tasks=[task_view],
        artifacts=[],
        thinking=True,
    )

    assert item["bucket"] == "ACTIVE"
    assert item["next_action"] == "No action is needed while the employees continue working."
    assert item["acceptance_criteria"] == ["Covers revenue and risks"]
    assert item["tasks"][0]["assignment_reason"] == "best match for source research"
    assert item["tasks"][0]["controls"]["pause"] is True


def test_failure_and_pause_are_operational_states_not_log_messages() -> None:
    failed = replace(
        Task.create("Collect figures"),
        status=TaskStatus.FAILED,
        error=TaskError(kind="ProviderError", message="The source is unavailable."),
    )
    failed_objective = replace(
        Objective.create("Prepare report"), status=ObjectiveStatus.FAILED
    )
    failed_view = views.work_task(
        failed, employee=None, calls=[], model_calls=[], objective_terminal=True
    )
    item = views.work_item(
        failed_objective, plans=[], tasks=[failed_view], artifacts=[], thinking=False
    )
    assert item["bucket"] == "FAILED"
    assert item["controls"]["retry"] is True
    assert failed_view["controls"]["handoff"] is True

    paused = replace(failed, status=TaskStatus.PAUSED, error=None)
    paused_objective = replace(failed_objective, status=ObjectiveStatus.PAUSED)
    paused_view = views.work_task(paused, employee=None, calls=[], model_calls=[])
    item = views.work_item(
        paused_objective, plans=[], tasks=[paused_view], artifacts=[], thinking=False
    )
    assert item["bucket"] == "BLOCKED"
    assert item["controls"]["resume"] is True
