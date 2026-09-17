"""In-memory task storage for tests and for running without a database file."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from domain.tasks.task import RESUMABLE_STATUSES, Task, TaskEvent
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class InMemoryTaskRepository:
    """Implements `domain.tasks.repository.TaskRepository`."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._events: dict[UUID, list[TaskEvent]] = {}

    async def save(self, task: Task, event: TaskEvent | None = None) -> None:
        self._tasks[task.id] = deepcopy(task)
        if event is not None:
            self._events.setdefault(task.id, []).append(event)

    async def get(self, task_id: UUID) -> Task | None:
        task = self._tasks.get(task_id)
        return deepcopy(task) if task else None

    async def list_resumable(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Task]:
        return [
            deepcopy(task)
            for task in sorted(
                self._tasks.values(), key=lambda t: (-t.priority, t.created_at)
            )
            if task.workspace_id == workspace_id and task.status in RESUMABLE_STATUSES
        ]

    async def list_in_status(
        self, status, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Task]:
        return [
            deepcopy(task)
            for task in sorted(self._tasks.values(), key=lambda t: t.created_at)
            if task.workspace_id == workspace_id and task.status is status
        ]

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Task]:
        newest = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        return [
            deepcopy(task) for task in newest if task.workspace_id == workspace_id
        ][:limit]

    async def events(self, task_id: UUID) -> list[TaskEvent]:
        return list(self._events.get(task_id, []))
