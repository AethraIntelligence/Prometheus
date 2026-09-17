"""Stopping a task that is already running.

There is already a brake in the platform - `prometheus stop`, the STOP file - and it
deliberately stops one thing: anything acting on a screen. It is a blunt,
machine-wide switch, read immediately before an irreversible physical action,
and it is right for that job precisely because it is blunt.

Cancelling a task is a different question with a different answer. It names one
task out of several that may be running, it has to leave the task in a legal
terminal state with whatever it had produced so far, and it must not stop a
second task that happens to be running beside it. So it is asked per task, and
checked where a run can be interrupted without losing work: between steps, and
before a tool call is made.

Cooperative, not pre-emptive. Killing a coroutine mid-tool leaves the outside
world in whatever state the tool had reached and the task row describing a step
that never finished - which is exactly the failure `resume` exists to avoid.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class CancellationSignal(Protocol):
    """Read by the runtime. Answers one question about one task."""

    def is_cancelled(self, task_id: UUID) -> bool: ...


class Cancellations(CancellationSignal, Protocol):
    """The interface side: asks for a task to stop, and clears the request.

    Separate from the read-only signal because the runtime must not be able to
    cancel itself, and an interface holding the registry says so in its type.
    """

    def cancel(self, task_id: UUID, reason: str = "") -> None: ...

    def reason_for(self, task_id: UUID) -> str: ...

    def clear(self, task_id: UUID) -> None: ...

    def pause(self, task_id: UUID) -> None: ...

    def resume(self, task_id: UUID) -> None: ...

    def is_paused(self, task_id: UUID) -> bool: ...

    async def wait_until_resumed(self, task_id: UUID) -> None: ...


class LiveTasks(Protocol):
    """Which tasks a runtime in this process is carrying right now.

    Asked by the emergency stop before it closes a task in the store: a task
    something here is running closes itself at its next boundary, and writing a
    terminal status underneath that loop is the conflation `Runs.cancel` exists
    to avoid. A task nobody here carries was left by a process that is gone.
    """

    def begin(self, task_id: UUID) -> None: ...

    def end(self, task_id: UUID) -> None: ...

    def carrying(self, task_id: UUID) -> bool: ...


class NeverCancelled:
    """The default: nothing is asking this run to stop."""

    def is_cancelled(self, task_id: UUID) -> bool:
        return False

    def is_paused(self, task_id: UUID) -> bool:
        return False

    async def wait_until_resumed(self, task_id: UUID) -> None:
        return None
