"""The emergency stop: one machine-wide state that forbids work until a person lifts it.

The STOP file existed before this and braked one thing - an action on a screen.
Phase 13 widens the same brake to everything the platform does, and keeps its
shape because the shape was right: a record beside the database that survives
the process it stops, can be set from a second terminal while the first one is
busy, and reads as *engaged* when it cannot be read at all.

Three decisions are worth stating.

**The state is the record, not a flag in memory.** A stop that lived in the
running process would be forgotten by the restart that follows a crash, and the
next start would resume exactly the work somebody pulled the brake on. So a
stopped installation starts stopped, and only an explicit release lets work
begin again.

**It is checked where the effect happens, not only where work begins.**
Forbidding new work and cancelling running work both happen, but neither closes
the gap between an approval being granted and the tool being called: a person
may click "approve" one millisecond after somebody else clicks "stop". The
executor reads the stop immediately before calling the tool, after the gate has
answered, so no approval can carry an action past a stop that was already set.

**Stopping never claims to have undone anything.** An email that was sent before
the brake was pulled stays sent. The report says what was stopped and what was
cancelled; it says nothing about reversing effects, because nothing does.

The format is versioned JSON. The earlier file held a bare reason, and it still
means "engaged" with that reason; a version this code does not know is engaged
too, because the two ways of being wrong about a brake are not symmetrical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

#: The version this code writes. A reader treats anything newer as engaged.
FORMAT_VERSION = 1
DEFAULT_REASON = "a person stopped all work"


@dataclass(frozen=True, slots=True)
class StopState:
    """Whether work is stopped, why, by whom and since when."""

    engaged: bool
    reason: str = ""
    engaged_by: str = ""
    engaged_at: datetime | None = None
    #: True when the record could not be read as this version writes it. Still
    #: engaged: an unreadable brake is a brake that is on.
    unreadable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "engaged": self.engaged,
            "reason": self.reason,
            "engaged_by": self.engaged_by,
            "engaged_at": self.engaged_at.isoformat() if self.engaged_at else None,
            "unreadable": self.unreadable,
        }


RELEASED = StopState(engaged=False)


def engaged(reason: str, by: str, at: datetime | None = None) -> StopState:
    return StopState(
        engaged=True,
        reason=reason.strip() or DEFAULT_REASON,
        engaged_by=by.strip() or "user",
        engaged_at=at or datetime.now(UTC),
    )


def render(state: StopState) -> str:
    """The text written to the record. Only an engaged state is ever written."""
    return json.dumps(
        {
            "version": FORMAT_VERSION,
            "reason": state.reason,
            "engaged_by": state.engaged_by,
            "engaged_at": (state.engaged_at or datetime.now(UTC)).isoformat(),
        },
        sort_keys=True,
    )


def parse(text: str) -> StopState:
    """Read a record that exists. Its existence alone means engaged.

    A plain-text record is the format installations before Phase 13 wrote, and
    its whole content is the reason. Anything that looks like JSON but is not a
    version this code understands is engaged with a reason saying so, never
    released.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return StopState(engaged=True, reason=stripped or DEFAULT_REASON)
    try:
        raw = json.loads(stripped)
    except ValueError:
        return StopState(
            engaged=True,
            reason="the stop record is damaged, so work stays stopped",
            unreadable=True,
        )
    if not isinstance(raw, dict) or raw.get("version") != FORMAT_VERSION:
        return StopState(
            engaged=True,
            reason="the stop record was written by a different version, so work stays stopped",
            unreadable=True,
        )
    at = raw.get("engaged_at")
    try:
        when = datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        when = None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return StopState(
        engaged=True,
        reason=str(raw.get("reason") or DEFAULT_REASON),
        engaged_by=str(raw.get("engaged_by") or ""),
        engaged_at=when,
    )


class Halter(Protocol):
    """Something outside the task loop that a stop has to reach directly.

    A task stops at its next safe boundary; a sandboxed program or an open
    connection to a service has no such boundary to reach. `halt` ends what it
    can and says how many it ended; `resume` undoes nothing that was done, it
    only makes the capability available again.
    """

    name: str

    async def halt(self) -> int: ...

    async def resume(self) -> None: ...


class EmergencyStop(Protocol):
    """The read side, held by everything that must not act while stopped.

    It is a `domain.computer.protocols.StopSignal` as well, so the screen guard
    that already reads the brake reads this one without learning a new word.
    """

    def state(self) -> StopState: ...

    def engaged(self) -> bool: ...

    @property
    def reason(self) -> str: ...


class EmergencyBrake(EmergencyStop, Protocol):
    """The side that can set and lift it. Held by the stop control and nothing else."""

    def engage(self, reason: str = "", *, by: str = "user") -> StopState: ...

    def release(self) -> bool: ...
