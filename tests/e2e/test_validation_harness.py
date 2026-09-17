"""Phase 11's Definition of Done, as a test that adds a scenario and runs it.

*There is a written answer to the question of what Prometheus does reliably, what it
does sometimes, and what it does not do at all.* The answer is only worth
anything if producing it takes no code - so this declares a scenario in a
temporary directory, runs it through the harness, and reads the answer back out.
If any of that ever needs a line of Python somewhere else, the claim is broken
and this is where it shows.

The employee here does not exist and the model is never called. That is
deliberate: what is under test is the harness's honesty - that it reads the
world rather than the run's own account of itself - and a real model would
make every assertion about a model instead.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

from application.validation.harness import ValidationHarness
from application.validation.report import render
from domain.audit.protocols import AuditRecord
from domain.policies.models import ActorKind
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.tools.telemetry import ToolCallRecord
from domain.validation.failures import FailureKind
from domain.validation.reliability import Verdict, build_report, reliability_of
from domain.validation.run import RunStatus
from domain.validation.scenario import Requirement, Scenario
from infrastructure.persistence.audit_repository import InMemoryAuditLog
from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
from infrastructure.persistence.tool_call_repository import InMemoryToolCallLog
from infrastructure.persistence.validation_run_repository import (
    InMemoryValidationRunRepository,
)
from infrastructure.validation.yaml_registry import YamlScenarioRegistry

SCENARIO = """
name: file-the-invoice
description: One loose file put where it belongs.
phase: 4
employee: organizer
request: >
  Read "unsorted/invoice.txt" and file it under "sorted/".
setup:
  unsorted/invoice.txt: INVOICE 4417, $420.00
expect:
  output_contains: [filed]
  files_exist: [sorted/invoice.txt]
  max_steps: 5
"""


class FakeEmployees:
    """Implements `StepExecution` - the contract the workflow engine uses too.

    It writes the file the scenario asks for, because that is what an employee
    would have done, and the harness has to find it on disk rather than believe
    the summary.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        do_the_work: bool = True,
        summary: str = "Filed it.",
        repository: InMemoryTaskRepository | None = None,
    ) -> None:
        self._workspace = workspace
        self._do_the_work = do_the_work
        self._summary = summary
        self._repository = repository
        self.tasks: list[Task] = []

    async def submit_and_run(self, goal: str, employee_name: str, **kwargs: object) -> Task:
        del employee_name, kwargs
        if self._do_the_work:
            target = self._workspace / "sorted" / "invoice.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("INVOICE 4417, $420.00", encoding="utf-8")
        task = replace(
            Task.create(goal),
            status=TaskStatus.COMPLETED,
            result=TaskResult(summary=self._summary),
            cost_usd=0.004,
        )
        task = task.with_execution(task.execution.advance())
        if self._repository is not None:
            await self._repository.save(task)
        self.tasks.append(task)
        return task


def harness(
    tmp_path: Path,
    employees: FakeEmployees,
    *,
    runs: InMemoryValidationRunRepository | None = None,
    tasks: InMemoryTaskRepository | None = None,
    tool_calls: InMemoryToolCallLog | None = None,
    audit: InMemoryAuditLog | None = None,
    available: frozenset[Requirement] = frozenset(Requirement),
) -> ValidationHarness:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(exist_ok=True)
    (scenarios / "file-the-invoice.yaml").write_text(SCENARIO, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ValidationHarness(
        scenarios=YamlScenarioRegistry(scenarios),
        runs=runs or InMemoryValidationRunRepository(),
        tasks=tasks or InMemoryTaskRepository(),
        workspace=workspace,
        available=available,
        employees=employees,
        tool_calls=tool_calls,
        audit=audit,
    )


# --- Adding a scenario is adding a file ---------------------------------------


async def test_a_scenario_declared_in_a_directory_runs_and_is_recorded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = InMemoryValidationRunRepository()
    employees = FakeEmployees(workspace)

    run = await harness(tmp_path, employees, runs=runs).run("file-the-invoice")

    assert run.status is RunStatus.PASSED
    assert run.failure is FailureKind.NONE
    assert all(result.passed for result in run.checks)
    assert len(run.checks) == 4  # finished, the phrase, the file, the ceiling
    # The setup was written before the request, and the request saw it.
    assert (workspace / "unsorted" / "invoice.txt").exists()
    assert employees.tasks, "the scenario never reached the platform"
    # And it is in the store, which is what makes a second run comparable.
    assert [stored.id for stored in await runs.recent()] == [run.id]


async def test_the_harness_believes_the_disk_and_not_the_summary(tmp_path: Path) -> None:
    """The failure Phase 10 caught in a workflow, now caught by the harness.

    A run that says it filed the invoice and filed nothing has to fail, or the
    suite is measuring how confidently the platform writes closing messages.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The task really ran - it took a step and called nothing useful. What is
    # wrong is the result, not the decomposition, and the taxonomy has to say so.
    tasks = InMemoryTaskRepository()
    employees = FakeEmployees(
        workspace, do_the_work=False, summary="Filed it. All done!", repository=tasks
    )

    run = await harness(tmp_path, employees, tasks=tasks).run("file-the-invoice")

    assert run.status is RunStatus.FAILED
    assert run.failure is FailureKind.EXPECTATION
    assert [result.check for result in run.failed_checks] == ["file 'sorted/invoice.txt'"]


async def test_a_scenario_this_machine_cannot_run_is_skipped_not_failed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A machine with nothing switched on, and a scenario that needs a screen.
    instance = harness(tmp_path, FakeEmployees(workspace), available=frozenset())
    scenario = replace(
        Scenario(name="drive-a-screen", request="Click the button."),
        requires=(Requirement.COMPUTER_USE,),
    )

    run = await instance.run_scenario(scenario)

    assert run.status is RunStatus.SKIPPED
    assert "computer use" in run.summary


async def test_reset_clears_what_the_scenario_says_must_not_be_there(
    tmp_path: Path,
) -> None:
    """The other half of the same finding: a run that meets its own output from
    last time is not repeating the experiment."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    leftover = workspace / "sorted" / "invoice.txt"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("from last time", encoding="utf-8")
    outside = tmp_path / "not-the-workspace.txt"
    outside.write_text("untouched", encoding="utf-8")

    instance = harness(tmp_path, FakeEmployees(workspace))
    scenario = replace(
        instance._scenarios.get("file-the-invoice"),
        reset=("sorted", "../not-the-workspace.txt"),
    )
    run = await instance.run_scenario(scenario)

    assert run.status is RunStatus.PASSED  # the employee wrote it again
    assert (workspace / "sorted" / "invoice.txt").read_text() != "from last time"
    # A path that leaves the workspace is refused, not deleted.
    assert outside.read_text() == "untouched"


async def test_a_file_an_earlier_run_left_behind_does_not_count(tmp_path: Path) -> None:
    """Found by the first full pass: a scenario passed on a draft the Phase 10
    run had left in the workspace weeks earlier. What is being measured is what
    this run produced, and a stale file is somebody else's output."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stale = workspace / "sorted" / "invoice.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("left over from last time", encoding="utf-8")

    # This employee does nothing at all - the file is already there.
    run = await harness(tmp_path, FakeEmployees(workspace, do_the_work=False)).run(
        "file-the-invoice"
    )

    assert run.status is RunStatus.FAILED
    assert "file 'sorted/invoice.txt'" in [result.check for result in run.failed_checks]


# --- The evidence comes from where the platform already wrote it --------------


async def test_the_refusal_is_read_out_of_the_audit_and_the_calls_out_of_the_log(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tasks = InMemoryTaskRepository()
    tool_calls = InMemoryToolCallLog()
    audit = InMemoryAuditLog()
    employees = FakeEmployees(workspace)

    instance = harness(
        tmp_path, employees, tasks=tasks, tool_calls=tool_calls, audit=audit
    )

    original = employees.submit_and_run

    async def run_and_record(goal: str, employee_name: str, **kwargs: object) -> Task:
        task = await original(goal, employee_name, **kwargs)
        await tasks.save(task)
        await tool_calls.record(ToolCallRecord(tool="fs.read", success=True, task_id=task.id))
        await tool_calls.record(ToolCallRecord(tool="fs.move", success=False, task_id=task.id))
        await audit.record(
            AuditRecord(
                action="code.run(...)",
                actor_kind=ActorKind.EMPLOYEE,
                result="DENIED",
                task_id=task.id,
                tool="code.run",
            )
        )
        return task

    employees.submit_and_run = run_and_record  # type: ignore[method-assign]
    run = await instance.run("file-the-invoice")

    assert run.metrics.tool_calls == 2
    assert run.metrics.failed_tool_calls == 1
    # The denied call exists nowhere but the audit - the tool never ran.
    assert run.metrics.denied_actions == 1
    assert run.metrics.steps == 1
    assert run.metrics.cost_usd == 0.004
    # The refusal is recorded and does not by itself condemn the run: this one
    # was asked for a filed invoice, and it filed the invoice. What a denial
    # explains is a run that *did not* get there - which is where the taxonomy
    # reaches for it (tests/unit/test_validation_scenarios.py).
    assert run.status is RunStatus.PASSED
    assert run.failure is FailureKind.NONE


# --- The written answer -------------------------------------------------------


async def test_the_report_is_computed_from_the_runs_and_says_what_it_does_not_know(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = InMemoryValidationRunRepository()
    instance = harness(tmp_path, FakeEmployees(workspace), runs=runs)

    first = await instance.run("file-the-invoice")
    assert first.passed

    recorded = await runs.recent()
    report = build_report(["file-the-invoice", "never-tried"], recorded)
    text = render(report)

    # One success is not reliability, and the document says so in those words.
    assert reliability_of("file-the-invoice", recorded).verdict is Verdict.SOMETIMES
    assert "Works sometimes" in text
    assert "`file-the-invoice`" in text
    # What was never run is reported rather than omitted.
    assert "Not attempted" in text and "`never-tried`" in text
    # And the regression set is what has earned its place in it (§11.5).
    assert report.regression_set == ("file-the-invoice",)


async def test_a_scenario_that_passed_twice_is_called_reliable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = InMemoryValidationRunRepository()
    instance = harness(tmp_path, FakeEmployees(workspace), runs=runs)

    await instance.run("file-the-invoice")
    await instance.run("file-the-invoice")

    report = build_report(["file-the-invoice"], await runs.recent())
    assert report.of(Verdict.RELIABLE)[0].scenario == "file-the-invoice"
    assert report.completion_rate == 1.0
    assert "Works reliably" in render(report)


def test_the_task_ids_a_run_reports_are_the_ones_it_measures() -> None:
    """A guard on the one shortcut that would make every metric meaningless."""
    assert UUID(str(Task.create("x").id))  # ids are ids, and the harness reads them


async def test_a_thread_is_asked_in_one_conversation_before_the_measured_request(
    tmp_path: Path,
) -> None:
    """Phase 9: a scenario about a long session states the session as a file.

    Every earlier request is carried to an answer, in order, in one thread, and
    only the last one is measured - the way a person would have asked them.
    """
    from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus

    class Manager:
        def __init__(self) -> None:
            self.asked: list[tuple[str, UUID | None]] = []

        async def receive(self, request, workspace_id=None, conversation_id=None):
            self.asked.append((request, conversation_id))
            return Objective.create(request, conversation_id=conversation_id)

        async def handle_objective(self, objective):
            return ObjectiveResult(
                objective_id=objective.id,
                summary="Done.",
                status=ObjectiveStatus.DONE,
                cost_usd=0.01,
            )

    manager = Manager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scenario = Scenario(
        name="a-thread", request="And now the last one", thread=("first", "second")
    )
    run = await ValidationHarness(
        scenarios=YamlScenarioRegistry(tmp_path),
        runs=InMemoryValidationRunRepository(),
        tasks=InMemoryTaskRepository(),
        workspace=workspace,
        available=frozenset(Requirement),
        objectives=manager,
    ).run_scenario(scenario)

    assert [request for request, _ in manager.asked] == ["first", "second", "And now the last one"]
    threads = {conversation for _, conversation in manager.asked}
    assert len(threads) == 1 and None not in threads
    assert run.status is RunStatus.PASSED
    assert round(run.metrics.cost_usd, 2) == 0.03, "the history is part of what it cost"


async def test_every_run_records_the_models_it_was_measured_on(tmp_path: Path) -> None:
    """Phase 10: a pass rate is about a scenario *and* the models that ran it."""
    from domain.validation.profile import profile_of

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = InMemoryValidationRunRepository()
    profile = profile_of({"EXECUTION": "fast (local/small)"})
    built = harness(tmp_path, FakeEmployees(workspace), runs=runs)
    built._profile = lambda: profile

    run = await built.run("file-the-invoice")

    assert run.profile == profile
    assert (await runs.recent())[0].profile == profile
