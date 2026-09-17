"""Persistence contracts for the manager's own record.

What the user asked for, what Prometheus decided it meant, and who was asked to do
what. No SQL crosses this boundary.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from domain.employees.definition import EmployeeId
from domain.workforce.assignment import TaskAssignment
from domain.workforce.protocols import Objective, Plan
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class AssignmentRepository(Protocol):
    async def save(self, assignment: TaskAssignment) -> None: ...

    async def get(self, assignment_id: UUID) -> TaskAssignment | None: ...

    async def for_task(self, task_id: UUID) -> list[TaskAssignment]:
        """Every assignment made for a task, newest first.

        A task can be assigned more than once - reassigned after a failure, or
        retried - and the history is what makes that legible afterwards.
        """
        ...

    async def for_employee(self, employee_id: EmployeeId) -> list[TaskAssignment]: ...


class ObjectiveRepository(Protocol):
    async def save(self, objective: Objective) -> None: ...

    async def get(self, objective_id: UUID) -> Objective | None: ...

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Objective]:
        """What has been asked for here lately, newest first."""
        ...

    async def for_conversation(self, conversation_id: UUID) -> list[Objective]:
        """One thread, oldest first - which is the order it was said in.

        The opposite order to `list_recent`, and deliberately: a history list
        is read from the top, a conversation is read from the beginning.
        """
        ...

    async def list_incomplete(self) -> list[Objective]:
        """Work left by a previous process, oldest first across workspaces."""
        ...


class PlanRepository(Protocol):
    """Plans and their task dependencies, stored together.

    One contract rather than two, because a plan without its dependency edges
    is not a plan - it is a list of tasks in an order nobody promised.
    """

    async def save(self, plan: Plan) -> None: ...

    async def get(self, plan_id: UUID) -> Plan | None: ...

    async def for_objective(self, objective_id: UUID) -> list[Plan]:
        """Every revision, newest first. A superseded plan is kept, not deleted.

        What Prometheus thought on the first attempt is the only evidence of why a
        second was needed.
        """
        ...
