"""Which tasks have been asked to stop, for as long as this process lives.

Deliberately not a file, and deliberately not a column. `prometheus stop` is a file
because it has to work from a second terminal while the first one holds the
screen. Cancelling a task is the opposite situation: the person asking is
already talking to the process that is running it, through the interface that
started it. A file would buy nothing and would then have to be cleaned up after
a crash, whereas a request that dies with the process is exactly right - a task
that was cancelled and then interrupted comes back through `resume` as the task
it was, and can be cancelled again.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from infrastructure.observability.logging import get_logger

log = get_logger(__name__)


class InMemoryCancellations:
    """Implements `domain.tasks.cancellation.Cancellations` and `LiveTasks`."""

    def __init__(self) -> None:
        self._reasons: dict[UUID, str] = {}
        self._paused: set[UUID] = set()
        self._wake: dict[UUID, asyncio.Event] = {}
        #: Runs in progress, counted: a task resumed while its first run is
        #: still unwinding is carried twice for a moment, not zero times.
        self._live: dict[UUID, int] = {}

    # --- Implements `domain.tasks.cancellation.LiveTasks` ----------------------

    def begin(self, task_id: UUID) -> None:
        self._live[task_id] = self._live.get(task_id, 0) + 1

    def end(self, task_id: UUID) -> None:
        remaining = self._live.get(task_id, 0) - 1
        if remaining > 0:
            self._live[task_id] = remaining
        else:
            self._live.pop(task_id, None)

    def carrying(self, task_id: UUID) -> bool:
        return task_id in self._live

    def cancel(self, task_id: UUID, reason: str = "") -> None:
        self._reasons[task_id] = reason
        self._event(task_id).set()
        log.info("task.cancel_requested", task_id=str(task_id), reason=reason)

    def is_cancelled(self, task_id: UUID) -> bool:
        return task_id in self._reasons

    def reason_for(self, task_id: UUID) -> str:
        return self._reasons.get(task_id, "")

    def clear(self, task_id: UUID) -> None:
        self._reasons.pop(task_id, None)
        self._paused.discard(task_id)
        self._wake.pop(task_id, None)

    def pause(self, task_id: UUID) -> None:
        self._paused.add(task_id)
        self._event(task_id).clear()
        log.info("task.pause_requested", task_id=str(task_id))

    def resume(self, task_id: UUID) -> None:
        self._paused.discard(task_id)
        self._event(task_id).set()
        log.info("task.resume_requested", task_id=str(task_id))

    def is_paused(self, task_id: UUID) -> bool:
        return task_id in self._paused

    async def wait_until_resumed(self, task_id: UUID) -> None:
        while self.is_paused(task_id) and not self.is_cancelled(task_id):
            await self._event(task_id).wait()

    def _event(self, task_id: UUID) -> asyncio.Event:
        return self._wake.setdefault(task_id, asyncio.Event())
