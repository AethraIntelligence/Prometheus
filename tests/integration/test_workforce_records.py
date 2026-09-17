"""Phase 11 records on a real store: decisions, verdicts, suggestions, and old rows."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import text

from application.workflows.suggestions import WorkflowSuggestions
from domain.errors import ConfigurationError, PrometheusError
from domain.policies.models import ActorKind
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.validation.run import RunStatus, ValidationRun
from domain.workflows.suggestion import (
    MIN_OCCURRENCES,
    StepShape,
    Suggestion,
    SuggestionStatus,
)
from domain.workforce.acceptance import Acceptance, AcceptanceCode
from domain.workforce.assignment import TaskAssignment
from domain.workforce.decision import Alternative, Decision, RejectionCode, SelectionCode
from domain.workforce.protocols import Objective, ObjectiveStatus, Plan, PlanStatus
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.assignment_repository import SqlAssignmentRepository
from infrastructure.persistence.employee_repository import SqlEmployeeRepository
from infrastructure.persistence.objective_repository import SqlObjectiveRepository
from infrastructure.persistence.plan_repository import SqlPlanRepository
from infrastructure.persistence.task_repository import SqlTaskRepository
from infrastructure.persistence.validation_run_repository import SqlValidationRunRepository
from infrastructure.persistence.workflow_suggestion_repository import SqlSuggestionRepository
from infrastructure.workflows.yaml_registry import YamlWorkflowRegistry
from infrastructure.workflows.yaml_writer import YamlWorkflowWriter
from tests.fakes.employees import definition
from tests.fakes.workforce import FakeRegistry

GATHERER = definition("gatherer", tools=frozenset({"fs.read"}))
WRITER = definition("writer", tools=frozenset({"fs.write"}))


async def _task(session_factory, employee=GATHERER, **changes) -> Task:
    await SqlEmployeeRepository(session_factory).sync([GATHERER, WRITER])
    task = replace(Task.create("Gather the figures"), assigned_employee_id=employee.id, **changes)
    await SqlTaskRepository(session_factory).save(task)
    return task


# --- Assignments --------------------------------------------------------------


async def test_a_decision_and_a_verdict_round_trip(session_factory) -> None:
    task = await _task(session_factory)
    repository = SqlAssignmentRepository(session_factory)
    decision = Decision(
        SelectionCode.MODEL_CHOSE_AMONG_EQUALS,
        "either would do",
        alternatives=(Alternative("writer", RejectionCode.NOT_CHOSEN, "ranked lower"),),
        indistinguishable=("gatherer", "writer"),
    )
    verdict = Acceptance(False, "no file", code=AcceptanceCode.EVIDENCE_MISSING)
    assignment = TaskAssignment.create(
        task_id=task.id,
        employee_id=GATHERER.id,
        assigned_by=ActorKind.PROMETHEUS,
        decision=decision,
    ).judged(verdict)

    await repository.save(assignment)
    [loaded] = await repository.for_task(task.id)

    assert loaded.decision == decision
    assert loaded.acceptance == verdict


async def test_a_row_written_before_decisions_were_kept_reads_as_unrecorded(
    session_factory,
) -> None:
    task = await _task(session_factory)
    repository = SqlAssignmentRepository(session_factory)
    await repository.save(
        TaskAssignment.create(task_id=task.id, employee_id=GATHERER.id, assigned_by=ActorKind.USER)
    )
    async with session_factory() as session:
        # What migration 041 leaves on an old row, and what a newer build might write.
        await session.execute(
            text("UPDATE task_assignments SET decision = '{\"version\": 7}', acceptance = NULL")
        )
        await session.commit()

    [loaded] = await repository.for_task(task.id)

    assert loaded.decision is None
    assert loaded.acceptance is None


async def test_assignments_for_an_employee_are_windowed_and_capped(session_factory) -> None:
    repository = SqlAssignmentRepository(session_factory)
    now = datetime.now(UTC)
    for days in (1, 2, 40):
        task = await _task(session_factory)
        await repository.save(
            replace(
                TaskAssignment.create(
                    task_id=task.id, employee_id=GATHERER.id, assigned_by=ActorKind.USER
                ),
                assigned_at=now - timedelta(days=days),
            )
        )

    recent = await repository.for_employee(GATHERER.id, since=now - timedelta(days=30))
    capped = await repository.for_employee(GATHERER.id, limit=1)

    assert len(recent) == 2
    assert len(capped) == 1


async def test_the_workspace_is_filtered_before_the_profile_limit(session_factory) -> None:
    repository = SqlAssignmentRepository(session_factory)
    now = datetime.now(UTC)
    current = await _task(session_factory)
    elsewhere = await _task(session_factory)
    await repository.save(
        replace(
            TaskAssignment.create(
                task_id=current.id,
                employee_id=GATHERER.id,
                assigned_by=ActorKind.USER,
                workspace_id=WorkspaceId("default"),
            ),
            assigned_at=now - timedelta(minutes=2),
        )
    )
    await repository.save(
        replace(
            TaskAssignment.create(
                task_id=elsewhere.id,
                employee_id=GATHERER.id,
                assigned_by=ActorKind.USER,
                workspace_id=WorkspaceId("other"),
            ),
            assigned_at=now - timedelta(minutes=1),
        )
    )

    found = await repository.for_employee(
        GATHERER.id, workspace_id=WorkspaceId("default"), limit=1
    )

    assert [assignment.task_id for assignment in found] == [current.id]


async def test_a_validation_run_keeps_which_employees_it_was_made_of(session_factory) -> None:
    repository = SqlValidationRunRepository(session_factory)
    run = ValidationRun.create(
        "a-team", RunStatus.PASSED, employee_ids=(str(GATHERER.id), str(WRITER.id))
    )
    await repository.save(run)

    [loaded] = await repository.recent(scenario="a-team")

    assert loaded.employee_ids == (str(GATHERER.id), str(WRITER.id))


# --- Suggestions --------------------------------------------------------------


def _suggestion(**changes) -> Suggestion:
    now = datetime.now(UTC)
    return replace(
        Suggestion(
            id=uuid4(),
            fingerprint="f" * 64,
            steps=(StepShape("gatherer"), StepShape("writer", ("FILE_ACCESS",), (0,))),
            sources=(uuid4(), uuid4(), uuid4()),
            first_seen=now,
            last_seen=now,
        ),
        **changes,
    )


async def test_a_suggestion_round_trips(session_factory) -> None:
    repository = SqlSuggestionRepository(session_factory)
    item = _suggestion(status=SuggestionStatus.DISMISSED, dismissed_sources=(uuid4(),))

    await repository.save(item)
    loaded = await repository.get(item.id)

    assert loaded is not None
    assert loaded.steps == item.steps
    assert loaded.sources == item.sources
    assert loaded.dismissed_sources == item.dismissed_sources
    assert loaded.status is SuggestionStatus.DISMISSED


async def test_an_unknown_status_reads_as_dismissed_and_a_newer_pattern_is_skipped(
    session_factory,
) -> None:
    repository = SqlSuggestionRepository(session_factory)
    unknown, newer = _suggestion(), _suggestion(fingerprint="e" * 64)
    await repository.save(unknown)
    await repository.save(newer)
    async with session_factory() as session:
        await session.execute(
            text("UPDATE workflow_suggestions SET status = 'ARCHIVED' WHERE id = :id"),
            {"id": str(unknown.id)},
        )
        await session.execute(
            text("UPDATE workflow_suggestions SET pattern_version = 9 WHERE id = :id"),
            {"id": str(newer.id)},
        )
        await session.commit()

    listed = await repository.list(unknown.workspace_id)

    assert [item.id for item in listed] == [unknown.id]
    assert listed[0].status is SuggestionStatus.DISMISSED


async def _succeeded_twice_step_run(session_factory, *, done: bool = True) -> UUID:
    """One objective: a gatherer, then a writer who waited on it, both accepted."""
    tasks = SqlTaskRepository(session_factory)
    assignments = SqlAssignmentRepository(session_factory)
    objective = replace(
        Objective.create("Summarise the figures in reports/"),
        status=ObjectiveStatus.DONE if done else ObjectiveStatus.FAILED,
        finished_at=datetime.now(UTC),
    )
    await SqlObjectiveRepository(session_factory).save(objective)
    gather, write = Task.create("Gather"), Task.create("Write")
    plan = Plan.create(
        objective.id,
        tasks=(gather, write),
        dependencies=((write.id, gather.id),),
        status=PlanStatus.DONE,
    )
    await SqlPlanRepository(session_factory).save(plan)
    for task, employee in ((gather, GATHERER), (write, WRITER)):
        finished = replace(
            task,
            plan_id=plan.id,
            assigned_employee_id=employee.id,
            status=TaskStatus.COMPLETED,
            result=TaskResult(summary="done"),
        )
        await tasks.save(finished)
        await assignments.save(
            TaskAssignment.create(
                task_id=task.id, employee_id=employee.id, assigned_by=ActorKind.PROMETHEUS
            ).judged(Acceptance.taken())
        )
    return objective.id


def _service(session_factory, directory: Path) -> WorkflowSuggestions:
    registry = YamlWorkflowRegistry(directory)
    return WorkflowSuggestions(
        suggestions=SqlSuggestionRepository(session_factory),
        objectives=SqlObjectiveRepository(session_factory),
        plans=SqlPlanRepository(session_factory),
        tasks=SqlTaskRepository(session_factory),
        assignments=SqlAssignmentRepository(session_factory),
        registry=FakeRegistry(GATHERER, WRITER),
        writer=YamlWorkflowWriter(directory),
        reload=registry.reload,
    )


async def test_one_or_failed_runs_suggest_nothing(session_factory, tmp_path: Path) -> None:
    await SqlEmployeeRepository(session_factory).sync([GATHERER, WRITER])
    service = _service(session_factory, tmp_path)
    await _succeeded_twice_step_run(session_factory)
    for _ in range(MIN_OCCURRENCES):
        await _succeeded_twice_step_run(session_factory, done=False)

    assert await service.refresh("default") == []


async def test_repeated_successful_structure_gives_one_draft_without_request_text(
    session_factory, tmp_path: Path
) -> None:
    await SqlEmployeeRepository(session_factory).sync([GATHERER, WRITER])
    service = _service(session_factory, tmp_path)
    sources = [await _succeeded_twice_step_run(session_factory) for _ in range(4)]

    first = await service.refresh("default")
    again = await service.refresh("default")

    assert len(first) == 1, "four runs of one structure are one suggestion"
    assert [item.suggestion.id for item in again] == [first[0].suggestion.id]
    shown = first[0]
    assert set(shown.suggestion.sources) == set(sources)
    assert [step.employee for step in shown.draft.steps] == ["gatherer", "writer"]
    assert shown.draft.steps[1].depends_on == ("step-1",)
    assert "figures" not in repr(shown.draft), "no request text is copied into the draft"


async def test_dismissing_holds_until_the_evidence_changes(session_factory, tmp_path: Path) -> None:
    await SqlEmployeeRepository(session_factory).sync([GATHERER, WRITER])
    service = _service(session_factory, tmp_path)
    for _ in range(MIN_OCCURRENCES):
        await _succeeded_twice_step_run(session_factory)
    [shown] = await service.refresh("default")

    await service.dismiss(shown.suggestion.id, "default")
    assert await service.refresh("default") == []

    await _succeeded_twice_step_run(session_factory)
    assert await service.refresh("default") == [], "one new run is not new evidence"

    for _ in range(MIN_OCCURRENCES - 1):
        await _succeeded_twice_step_run(session_factory)
    [returned] = await service.refresh("default")
    assert returned.suggestion.id == shown.suggestion.id, "the same suggestion, not a new one"


async def test_saving_writes_a_manual_workflow_once_and_never_over_another(
    session_factory, tmp_path: Path
) -> None:
    await SqlEmployeeRepository(session_factory).sync([GATHERER, WRITER])
    service = _service(session_factory, tmp_path)
    for _ in range(MIN_OCCURRENCES):
        await _succeeded_twice_step_run(session_factory)
    [shown] = await service.refresh("default")

    saved, where = await service.save(shown.suggestion.id, "default", name="figures-digest")

    written = yaml.safe_load(Path(where).read_text(encoding="utf-8"))
    assert written["trigger"] == "MANUAL"
    assert "schedule" not in Path(where).read_text(encoding="utf-8").split("#")[-1]
    loaded = YamlWorkflowRegistry(tmp_path).get("figures-digest")
    assert [step.employee for step in loaded.steps] == ["gatherer", "writer"]
    assert saved.status is SuggestionStatus.SAVED
    assert await service.refresh("default") == [], "a saved suggestion is not offered again"
    with pytest.raises(PrometheusError):
        await service.save(shown.suggestion.id, "default", name="figures-digest-2")


def test_the_writer_refuses_a_name_that_is_taken(tmp_path: Path) -> None:
    (tmp_path / "weekly.yaml").write_text("name: weekly\n", encoding="utf-8")
    from domain.workflows.definition import WorkflowDefinition, WorkflowStep

    with pytest.raises(ConfigurationError, match="already exists"):
        YamlWorkflowWriter(tmp_path).write(
            WorkflowDefinition(name="weekly", steps=(WorkflowStep("a", "gatherer", "do"),))
        )
    assert (tmp_path / "weekly.yaml").read_text(encoding="utf-8") == "name: weekly\n"


def test_writing_the_same_confirmed_draft_twice_recovers_idempotently(tmp_path: Path) -> None:
    from domain.workflows.definition import WorkflowDefinition, WorkflowStep

    writer = YamlWorkflowWriter(tmp_path)
    definition = WorkflowDefinition(
        name="weekly", steps=(WorkflowStep("a", "gatherer", "do"),)
    )

    first = writer.write(definition)
    second = writer.write(definition)

    assert first == second
    assert len(list(tmp_path.glob("weekly*.yaml"))) == 1
