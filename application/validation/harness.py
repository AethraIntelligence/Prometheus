"""Runs a declared scenario the way a person would, and records what happened.

Phase 11's whole claim is that this measures the product rather than a model of
it, and that claim rests on one restraint: **the harness has no way to do work.**
It holds the three contracts a user's request travels through - the manager, the
task runner, the workflow engine - and calls one of them. It cannot plan, cannot
choose an employee, cannot call a tool, and cannot approve anything. Whatever it
reports is therefore a fact about what a person would have got.

What it does own is the bookkeeping nobody else has a reason to do:

* **it prepares the world and then leaves it alone.** `setup` writes files into
  the workspace before the request; nothing is written during, and nothing is
  cleaned up after. A run that goes wrong leaves the evidence where a person can
  look at it, which is worth more than a tidy directory.
* **it gathers the evidence from where the platform already put it** - the task
  rows, `tool_calls`, `audit_log`, the approvals table. Not from the objects the
  run returned: an answer that says it wrote a file is exactly the failure mode
  this phase exists to catch, and only the store can contradict it.
* **it decides nothing about what the answer should be.** The expectations were
  written before the run, and `domain/validation/` compares them to the evidence
  as a pure function. This module is what makes the two meet.

A scenario the machine cannot run is skipped, with the reason. A suite that
reports failures for a switched-off desktop teaches people to stop reading it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from uuid import UUID, uuid4

import structlog

from application.knowledge.service import KnowledgeService
from application.workspaces.service import WorkspaceService
from domain.approvals.models import ApprovalState
from domain.approvals.protocols import ApprovalRepository
from domain.audit.protocols import AuditRecord, AuditTrail
from domain.errors import ConfigurationError
from domain.memory.models import MemoryQuery
from domain.memory.protocols import Memory
from domain.tasks.repository import TaskRepository
from domain.tasks.task import TaskCreatedBy, TaskStatus
from domain.tools.telemetry import ToolCallLog
from domain.validation.evidence import Evidence, Metrics, check
from domain.validation.failures import classify
from domain.validation.protocols import (
    Approver,
    ObjectiveExecution,
    ScenarioRegistry,
    WorkflowExecution,
)
from domain.validation.run import RunStatus, ValidationRun, ValidationRunRepository, outcome_of
from domain.validation.scenario import Entry, Requirement, Scenario
from domain.workflows.protocols import StepExecution
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: What the audit log calls an action that did not happen.
DENIED = "DENIED"


class ValidationHarness:
    """Implements running a scenario. Nothing here decides how work is done."""

    def __init__(
        self,
        *,
        scenarios: ScenarioRegistry,
        runs: ValidationRunRepository,
        tasks: TaskRepository,
        workspace: Path,
        available: frozenset[Requirement] = frozenset(),
        objectives: ObjectiveExecution | None = None,
        employees: StepExecution | None = None,
        workflows: WorkflowExecution | None = None,
        tool_calls: ToolCallLog | None = None,
        audit: AuditTrail | None = None,
        approvals: ApprovalRepository | None = None,
        memory: Memory | None = None,
        knowledge: KnowledgeService | None = None,
        workspaces: WorkspaceService | None = None,
        approver: Approver | None = None,
    ) -> None:
        self._scenarios = scenarios
        self._runs = runs
        self._tasks = tasks
        self._workspace = workspace
        self._available = available
        self._objectives = objectives
        self._employees = employees
        self._workflows = workflows
        self._tool_calls = tool_calls
        self._audit = audit
        self._approvals = approvals
        self._memory = memory
        # Both optional, both only used to *prepare* a scenario: a workspace
        # that has to exist, and documents that have to be in it. Neither can
        # do work, which is the restraint the whole harness rests on.
        self._knowledge = knowledge
        self._workspaces = workspaces
        # Who answers the questions the platform decides to ask. Not a way to
        # do work: the scenario declared the answer before the run, and this
        # only holds it for the length of one.
        self._approver = approver

    # --- Running --------------------------------------------------------------

    async def run(
        self, name: str, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> ValidationRun:
        return await self.run_scenario(self._scenarios.get(name), workspace_id=workspace_id)

    async def run_scenario(
        self, scenario: Scenario, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> ValidationRun:
        """One attempt, recorded whatever the outcome - including a skip."""
        missing = scenario.missing_requirements(self._available)
        if missing:
            return await self._record(
                ValidationRun.create(
                    scenario.name,
                    RunStatus.SKIPPED,
                    summary=(
                        "This machine has no "
                        + ", ".join(item.value.lower().replace("_", " ") for item in missing)
                        + "."
                    ),
                    workspace_id=workspace_id,
                    finished_at=datetime.now(UTC),
                )
            )

        # A scenario may say which workspace the request is made in - that is
        # the whole of Phase 15's Definition of Done: the document is over
        # there and the question is asked here. The *record* stays in the
        # workspace the harness was called for: a validation run is the
        # platform's account of itself, and one filed under the workspace a
        # scenario happened to name would vanish from the report for this
        # machine, which is where it was first noticed.
        asked_in = WorkspaceId(scenario.workspace) if scenario.workspace else workspace_id

        started = datetime.now(UTC)
        clock = time.monotonic()
        self._prepare(scenario)
        await self._prepare_knowledge(scenario)
        before = self._fingerprint(scenario)
        recalled = await self._recalled(scenario, asked_in)

        error: Exception | None = None
        summary = ""
        succeeded = False
        missing_from_run: tuple[str, ...] = ()
        task_ids: tuple[UUID, ...] = ()
        cost = 0.0
        try:
            with self._answering(scenario):
                summary, succeeded, missing_from_run, task_ids, cost = await self._ask(
                    scenario, asked_in
                )
        except Exception as caught:  # a scenario that raises is a finding, not a crash
            error = caught
            log.warning(
                "validation.run_failed",
                scenario=scenario.name,
                error_type=type(caught).__name__,
                error=str(caught),
            )

        evidence = await self._evidence(
            scenario,
            summary=summary,
            succeeded=succeeded,
            missing=missing_from_run,
            task_ids=task_ids,
            cost=cost,
            recalled=recalled,
            elapsed=time.monotonic() - clock,
            error=error,
            before=before,
        )
        results = check(scenario.expect, evidence)
        failure = classify(
            evidence,
            results,
            error=error,
            memory_expected=Requirement.MEMORY in scenario.requires,
            # Both lists say a refusal here is the platform behaving: one
            # declares the call that must be refused, the other the tool that
            # must not have run. Neither is a person the run needed.
            expected_denials=frozenset(scenario.expect.tools_denied)
            | frozenset(scenario.expect.tools_forbidden),
        )
        run = outcome_of(
            scenario.name,
            evidence,
            results,
            failure,
            workspace_id=workspace_id,
            started_at=started,
        )
        log.info(
            "validation.finished",
            scenario=scenario.name,
            status=run.status.value,
            failure=run.failure.value,
            steps=run.metrics.steps,
            cost_usd=round(run.metrics.cost_usd, 6),
        )
        return await self._record(run)

    async def run_all(
        self,
        scenarios: list[Scenario],
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> list[ValidationRun]:
        """One after another, and a failure never stops the rest.

        The point of a suite run is the shape of what fails, and a suite that
        stops at the first failure reports one data point per session.
        """
        return [
            await self.run_scenario(scenario, workspace_id=workspace_id)
            for scenario in scenarios
        ]

    # --- The three doors ------------------------------------------------------

    @contextmanager
    def _answering(self, scenario: Scenario) -> Iterator[None]:
        """Put the scenario's declared answers in front of the gate, and only here.

        With no approver configured the run is unattended, which is what every
        run was before scenarios could say otherwise - and still the right
        default: a machine nobody asked is a machine that refuses.
        """
        if self._approver is None:
            yield
            return
        with self._approver.answering(frozenset(scenario.approve)):
            yield


    async def _ask(
        self, scenario: Scenario, workspace_id: WorkspaceId
    ) -> tuple[str, bool, tuple[str, ...], tuple[UUID, ...], float]:
        """Make the request, and report only what the caller handed back.

        Everything measured comes from the store afterwards. What is taken from
        here is what the store cannot know: whether the platform itself
        considered the request answered, and in whose words.
        """
        if scenario.entry is Entry.OBJECTIVE:
            if self._objectives is None:
                raise _unavailable("the manager")
            conversation_id = uuid4() if scenario.thread else None
            spent = 0.0
            for earlier in scenario.thread:
                # The history the measured request depends on, asked the way a
                # person would have asked it: one request at a time, each
                # answered before the next, all in one thread.
                previous = await self._objectives.receive(
                    earlier, workspace_id, conversation_id=conversation_id
                )
                spent += (await self._objectives.handle_objective(previous)).cost_usd
            objective = await self._objectives.receive(
                scenario.request,
                workspace_id,
                **({"conversation_id": conversation_id} if conversation_id else {}),
            )
            result = await self._objectives.handle_objective(objective)
            ids = tuple(
                UUID(str(entry["id"]))
                for entry in result.output.get("tasks", [])
                if entry.get("id")
            )
            return (
                result.summary,
                result.succeeded,
                result.missing,
                ids,
                result.cost_usd + spent,
            )

        if scenario.entry is Entry.TASK:
            if self._employees is None:
                raise _unavailable("the task runner")
            task = await self._employees.submit_and_run(
                scenario.request,
                scenario.target,
                created_by=TaskCreatedBy.USER,
                workspace_id=workspace_id,
            )
            return (
                task.result.summary if task.result else "",
                task.status is TaskStatus.COMPLETED,
                (),
                (task.id,),
                task.cost_usd,
            )

        if self._workflows is None:
            raise _unavailable("the workflow engine")
        run = await self._workflows.run(scenario.target, inputs=dict(scenario.inputs))
        return (
            "\n".join(step.summary for step in run.steps if step.summary),
            run.succeeded,
            tuple(step.step for step in run.steps if not step.succeeded),
            tuple(step.task_id for step in run.steps if step.task_id),
            0.0,
        )

    # --- The world before, and the evidence after -----------------------------

    def _prepare(self, scenario: Scenario) -> None:
        """Clear what the scenario says must not be there, then write its files.

        A scenario that ran yesterday left its output beside its input, and the
        run that follows has to start from the same place the first one did or
        the two are not comparable. `reset` is how a scenario says so, and it is
        the only thing here that deletes: paths named in a declaration, resolved
        inside the workspace, and never the workspace itself.
        """
        root = self._workspace.resolve()
        for relative in scenario.reset:
            path = (self._workspace / relative).resolve()
            # The declaration was checked at load; this is the check that
            # matters, because a symlink inside the workspace can still point
            # out of it and only resolving both ends can tell.
            if path == root or root not in path.parents:
                log.warning("validation.reset_refused", scenario=scenario.name, path=relative)
                continue
            if path.is_dir():
                rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        for relative, text in scenario.setup.items():
            path = self._workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    async def _prepare_knowledge(self, scenario: Scenario) -> None:
        """Create the workspaces a scenario names and put its documents in them.

        Adding the same text twice is one document (the checksum decides), so a
        scenario re-run does not accumulate copies of its own fixture - which is
        the knowledge equivalent of `reset`, and comes free from how documents
        are stored rather than needing a rule here.

        Guarded: a machine with knowledge switched off runs the scenario without
        the documents and fails its expectations honestly, which is a truer
        report than a skip that hides the configuration.
        """
        if not scenario.knowledge:
            return
        if self._knowledge is None or self._workspaces is None:
            log.warning("validation.knowledge_unavailable", scenario=scenario.name)
            return
        for workspace, documents in scenario.knowledge.items():
            target = WorkspaceId(workspace)
            if await self._workspaces.get(target) is None:
                await self._workspaces.create(workspace)
            for title, text in documents.items():
                await self._knowledge.add_text(text, title=title, workspace_id=target)

    async def _recalled(self, scenario: Scenario, workspace_id: WorkspaceId) -> int:
        """How much memory has to offer this request, asked before it runs.

        Not a measure of what the run used - nothing records that - but of what
        was there to use. For a scenario that depends on remembering, zero is
        the finding: the second run had nothing the first one left it, and no
        amount of good planning was going to recover the difference.
        """
        if self._memory is None or Requirement.MEMORY not in scenario.requires:
            return 0
        try:
            found = await self._memory.recall(
                MemoryQuery(text=scenario.request, workspace_id=workspace_id, limit=20)
            )
        except Exception as error:  # measuring must never fail the measurement
            log.warning("validation.recall_failed", scenario=scenario.name, error=str(error))
            return 0
        return len(found)

    async def _evidence(
        self,
        scenario: Scenario,
        *,
        summary: str,
        succeeded: bool,
        missing: tuple[str, ...],
        task_ids: tuple[UUID, ...],
        cost: float,
        recalled: int,
        elapsed: float,
        error: Exception | None,
        before: dict[str, float | None],
    ) -> Evidence:
        tasks = [task for task in [await self._tasks.get(task_id) for task_id in task_ids] if task]
        used, failed, calls = await self._tool_evidence(task_ids)
        denied = await self._denied(task_ids)
        asked, answered = await self._approval_evidence(task_ids)

        present, text = self._files(scenario, before)
        return Evidence(
            succeeded=succeeded,
            summary=summary,
            missing=missing,
            metrics=Metrics(
                steps=sum(task.execution.step for task in tasks),
                # The manager reports the objective's cost; a task or a workflow
                # has none of its own, so the tasks are added up instead.
                cost_usd=cost or sum(task.cost_usd for task in tasks),
                duration_seconds=round(elapsed, 3),
                tasks=len(tasks) or len(task_ids),
                tool_calls=calls,
                failed_tool_calls=len(failed),
                denied_actions=len(denied),
                approvals_requested=asked,
                interventions=answered,
                recalled=recalled,
            ),
            employees=len(
                {task.assigned_employee_id for task in tasks if task.assigned_employee_id}
            ),
            tools_used=used,
            tools_failed=failed,
            tools_denied=denied,
            files_present=present,
            file_text=text,
            error_type=type(error).__name__ if error else "",
            error_message=str(error) if error else "",
        )

    async def _tool_evidence(
        self, task_ids: tuple[UUID, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...], int]:
        if self._tool_calls is None:
            return (), (), 0
        used: list[str] = []
        failed: list[str] = []
        total = 0
        for task_id in task_ids:
            for record in await self._tool_calls.list_for_task(task_id):
                total += 1
                (used if record.success else failed).append(record.tool)
        return tuple(dict.fromkeys(used)), tuple(dict.fromkeys(failed)), total

    async def _denied(self, task_ids: tuple[UUID, ...]) -> tuple[str, ...]:
        """Which tools the platform refused. The audit is the only place this is.

        A denied call leaves no tool call - the tool never ran - so a harness
        that read `tool_calls` alone would report a run in which the brake held
        as a run in which nothing was ever attempted.
        """
        if self._audit is None:
            return ()
        names: list[str] = []
        for task_id in task_ids:
            records: list[AuditRecord] = await self._audit.recent(limit=200, task_id=task_id)
            names.extend(
                record.tool for record in records if record.result == DENIED and record.tool
            )
        return tuple(dict.fromkeys(names))

    async def _approval_evidence(self, task_ids: tuple[UUID, ...]) -> tuple[int, int]:
        """How often somebody was asked, and how often somebody answered.

        The two differ, and the difference is the point (§11.4). An unattended
        run that was refused by default needed no person; counting it as an
        intervention would say the platform needs a human where what it needs is
        a policy. Only a resolution with a decision behind it counts.
        """
        if self._approvals is None:
            return 0, 0
        asked = 0
        answered = 0
        for task_id in task_ids:
            for approval in await self._approvals.for_task(task_id):
                asked += 1
                if approval.state in (ApprovalState.APPROVED, ApprovalState.REJECTED) and (
                    approval.resolved_by not in (None, "", "timeout", "no-approver")
                ):
                    answered += 1
        return asked, answered

    def _fingerprint(self, scenario: Scenario) -> dict[str, float | None]:
        """What the expected paths looked like before the request was made.

        Found by the first full pass, which passed a scenario on a draft the
        Phase 10 run had left in the workspace three weeks earlier. A file that
        was already there is not evidence that this run produced it, and a
        harness that cannot tell the difference reports the workspace's history
        as today's capability.
        """
        marks: dict[str, float | None] = {}
        for relative in scenario.expect.named_files:
            path = self._workspace / relative
            marks[relative] = path.stat().st_mtime if path.exists() else None
        return marks

    def _files(
        self, scenario: Scenario, before: dict[str, float | None]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Only the paths the expectations named, and only if this run touched them.

        A directory listing would be tempting and wrong: what matters is whether
        the file the scenario asked for is there, and a walk of the workspace
        would grow with every run that ever happened in it.
        """
        present: list[str] = []
        text: dict[str, str] = {}
        for relative in scenario.expect.named_files:
            path = self._workspace / relative
            if not path.exists():
                continue
            was = before.get(relative)
            if was is not None and path.stat().st_mtime == was:
                # Untouched since before the request. It is somebody else's
                # output, and counting it would be the suite flattering itself.
                log.info("validation.stale_file", scenario=scenario.name, path=relative)
                continue
            present.append(relative)
            if relative in scenario.expect.file_contains and path.is_file():
                try:
                    text[relative] = path.read_text(encoding="utf-8", errors="replace")
                except OSError as error:
                    log.warning("validation.unreadable", path=relative, error=str(error))
        return tuple(present), text

    async def _record(self, run: ValidationRun) -> ValidationRun:
        await self._runs.save(run)
        return run


def _unavailable(what: str) -> ConfigurationError:
    """A door this harness was not given. Configuration, and classified as such."""
    return ConfigurationError(f"This scenario goes through {what}, which is not wired up here.")
