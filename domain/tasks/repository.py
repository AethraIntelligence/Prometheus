"""Persistence contract for tasks. No SQL ever crosses this boundary."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from domain.tasks.task import Task, TaskEvent, TaskStatus
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class TaskRepository(Protocol):
    async def save(self, task: Task, event: TaskEvent | None = None) -> None:
        """Persist the task and, when a transition produced one, its event."""
        ...

    async def get(self, task_id: UUID) -> Task | None: ...

    async def list_resumable(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Task]:
        """Tasks that can be picked up again after a restart."""
        ...

    async def list_in_status(
        self, status: TaskStatus, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Task]:
        """Every task in one status, oldest first. What recovery reconciles."""
        ...

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Task]:
        """The latest tasks, newest first, whatever state they ended in.

        History is not the same question as resumability: an interface showing
        what has been run needs the failures and the finished work too, and it
        needs a bound, because a machine that has been used for a year should
        not load a year to draw a list.
        """
        ...

    async def events(self, task_id: UUID) -> list[TaskEvent]: ...
