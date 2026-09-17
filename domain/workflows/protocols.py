"""How the engine reaches everything outside itself.

Two contracts, and neither of them is the employee runtime. A workflow engine
that imported the runtime would be a second way to run a task, and the whole
design rests on there being one - so it asks for a task to be run exactly the
way the manager does, through a protocol somebody else implements.
"""

from __future__ import annotations

from typing import Protocol

from domain.tasks.task import Task
from domain.workflows.definition import WorkflowDefinition


class WorkflowRegistry(Protocol):
    """The only way the engine learns which workflows exist.

    Same shape as the employee registry, and for the same reason: adding a
    workflow is adding a declaration, so nothing may hold a list of them.
    """

    def list_all(self) -> list[WorkflowDefinition]: ...

    def get(self, name: str, version: int | None = None) -> WorkflowDefinition: ...


class StepExecution(Protocol):
    """Running one step, which is running one task.

    Deliberately the signature `TaskRunner` already has, so the runner satisfies
    this without an adapter and a test can satisfy it without a model.
    """

    async def submit_and_run(self, goal: str, employee_name: str, **kwargs: object) -> Task: ...
