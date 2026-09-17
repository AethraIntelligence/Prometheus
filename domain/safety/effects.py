"""Whether something is reaching the world right now, and holding the next one back.

An update replaces the program and restarts it. Restarting between two steps of
a task is safe - the task is durable and resumes - but restarting *inside* a
tool call that sends a message or deletes a file is the one moment whose outcome
nobody can reconstruct: the ledger says it started, and nothing says whether it
finished. So an update waits for a safe point, defined narrowly:

**No effect other than a read is executing.** Reads are free to interrupt.

And a safe point has to stay safe until the process is gone, so preparing for an
update *holds* the gate: a call that reaches it waits, parked before the effect,
exactly where a restart can pick it up. A hold lives in memory and dies with the
process - the new version starts with nothing held, and never needs to be told.

This is not the emergency stop. A stop cancels work and survives restarts; a
hold cancels nothing and does not survive the restart it was made for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EffectInFlight:
    task_id: UUID
    tool: str
    effect: str
    since: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "tool": self.tool,
            "effect": self.effect,
            "since": self.since.isoformat(),
        }


class EffectGate(Protocol):
    """Every non-read tool call passes through it; an update holds it."""

    def entering(self, task_id: UUID, tool: str, effect: str):
        """An async context manager around one effect. Waits while the gate is held."""
        ...

    def in_flight(self) -> tuple[EffectInFlight, ...]: ...

    def hold(self) -> None: ...

    def release(self) -> None: ...

    @property
    def held(self) -> bool: ...

    async def wait_idle(self, timeout_seconds: float) -> bool:
        """True once nothing is in flight, False if the timeout came first."""
        ...
