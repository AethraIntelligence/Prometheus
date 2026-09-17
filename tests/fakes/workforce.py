"""Doubles for the manager's collaborators.

Prometheus is the one component whose whole job is deciding, so its tests need a
workforce that does not exist and an executor that does not run - which is
exactly what `EmployeeRegistry` and `TaskExecution` being contracts buys.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from uuid import UUID, uuid4

from domain.capabilities.models import CapabilityRequirement
from domain.employees.definition import EmployeeDefinition
from domain.errors import EmployeeNotFoundError
from domain.policies.models import ActorKind
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.tools.refusals import REFUSED
from domain.workforce.assignment import TaskAssignment
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class FakeRegistry:
    """Implements `domain.employees.protocols.EmployeeRegistry`."""

    def __init__(self, *definitions: EmployeeDefinition) -> None:
        self._definitions = list(definitions)

    def list(self, workspace: WorkspaceId = DEFAULT_WORKSPACE_ID) -> list[EmployeeDefinition]:
        return [d for d in self._definitions if d.workspace_id == workspace and d.enabled]

    def get(self, name: str) -> EmployeeDefinition:
        for definition in self._definitions:
            if definition.name == name:
                return definition
        raise EmployeeNotFoundError(name)

    def find_by_capability(
        self, requirement: CapabilityRequirement
    ) -> list[EmployeeDefinition]:
        return [
            d
            for d in self.list()
            if requirement.is_satisfied_by(d.model_profile.capabilities)
        ]


class RecordingExecution:
    """Implements `domain.workforce.protocols.TaskExecution`.

    Records what it was asked to run and returns whatever the script says. The
    default is a task that completed with a summary naming its own goal, which
    is enough for every test that is about the manager rather than the work.
    """

    def __init__(
        self,
        outcome: Callable[[Task, TaskAssignment], Task] | None = None,
    ) -> None:
        self._outcome = outcome or _completed
        self.started: list[tuple[Task, TaskAssignment]] = []

    @property
    def employees(self) -> list[UUID]:
        return [assignment.employee_id for _, assignment in self.started]

    @property
    def goals(self) -> list[str]:
        return [task.goal for task, _ in self.started]

    async def start(self, task: Task, assignment: TaskAssignment) -> Task:
        self.started.append((task, assignment))
        return self._outcome(task, assignment)

    async def resume(self, task: Task) -> Task:
        assignment = TaskAssignment.create(
            task_id=task.id,
            employee_id=task.assigned_employee_id or uuid4(),
            assigned_by=ActorKind.PROMETHEUS,
            workspace_id=task.workspace_id,
        )
        self.started.append((task, assignment))
        return self._outcome(task, assignment)


def _completed(task: Task, assignment: TaskAssignment) -> Task:
    del assignment
    return replace(
        task,
        status=TaskStatus.COMPLETED,
        result=TaskResult(summary=f"Done: {task.goal}"),
        cost_usd=0.01,
    )


def failing(kind: str = "ExecutionError", message: str = "it did not work"):
    """An executor whose every task fails the same way."""

    def _fail(task: Task, assignment: TaskAssignment) -> Task:
        del assignment
        from domain.tasks.task import TaskError

        return replace(
            task,
            status=TaskStatus.FAILED,
            error=TaskError(kind=kind, message=message),
            result=TaskResult(summary=""),
        )

    return _fail


def refused_a_tool(tool: str = "web.search", *, approval: bool = False):
    """An executor whose task failed because a call never reached its tool.

    Shaped like what the executor actually records, `details[REFUSED]` and all:
    the supervisor used to read the refusal out of the summary sentence, and a
    fake that only wrote the sentence was agreeing with the wrong half of the
    contract. `approval` switches the sentence to the other way a call is
    refused - a person said no - which is the case Phase 18 found unhandled.
    """

    def _refuse(task: Task, assignment: TaskAssignment) -> Task:
        del assignment
        from domain.tasks.task import TaskError

        return replace(
            task,
            status=TaskStatus.FAILED,
            error=TaskError(kind="VerificationFailed", message="nothing was found"),
            result=TaskResult(
                summary="I could not look anything up.",
                output={
                    "observations": [
                        {
                            "step": 1,
                            "succeeded": False,
                            "summary": (
                                f"{tool} was not approved."
                                if approval
                                else f"{tool} failed: EMPLOYEE '{uuid4()}' may not use '{tool}'"
                            ),
                            "details": {"tool": tool, REFUSED: True},
                        }
                    ]
                },
            ),
        )

    return _refuse
