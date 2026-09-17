"""The brake, as a file.

A run that is acting on the user's machine needs a way to be stopped by the
user, from outside, at a moment nobody scheduled. The process is busy; it may
have the screen; the terminal that started it may be gone. What is left that
still works is the filesystem.

So: a sentinel path. `prometheus stop` creates it, `prometheus stop --clear`
removes it, and every action that reaches the world reads it first. Since Phase
13 it stops all work rather than only work on a screen, and its contents are a
versioned record (`domain.safety.emergency`) - a file holding a bare reason,
which is what earlier versions wrote, still reads as engaged with that reason.

Deliberately a file rather than a signal or a socket: it survives the process it
stops, so a stop the user set while nothing was running still holds when the
next run starts, and the interface sets it the same way the CLI does.

Written to a neighbour, flushed and moved into place: a brake that a full disk
or a crash left half-written must not read as released, and a rename is either
there or not.
"""

from __future__ import annotations

import os
from pathlib import Path

from domain.safety import emergency
from domain.safety.emergency import StopState
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

STOP_FILE_NAME = "STOP"
DEFAULT_REASON = emergency.DEFAULT_REASON


class FileStopSignal:
    """Implements `domain.safety.emergency.EmergencyBrake` and `StopSignal`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._reason = DEFAULT_REASON

    def state(self) -> StopState:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return emergency.RELEASED
        except (OSError, UnicodeDecodeError) as error:
            # An unreadable brake is a brake that is on. The alternative is
            # deciding that an I/O error means "carry on".
            log.warning("safety.stop_file_unreadable", path=str(self.path), error=str(error))
            return StopState(
                engaged=True,
                reason=f"the stop file at {self.path} could not be read",
                unreadable=True,
            )
        return emergency.parse(text)

    def engaged(self) -> bool:
        state = self.state()
        if state.engaged:
            self._reason = state.reason
        return state.engaged

    @property
    def reason(self) -> str:
        return self._reason

    # --- Setting and clearing -------------------------------------------------

    def engage(self, reason: str = "", *, by: str = "user") -> StopState:
        state = emergency.engaged(reason, by)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(emergency.render(state))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        self._reason = state.reason
        log.warning("safety.stop_engaged", path=str(self.path), reason=state.reason, by=by)
        return state

    def release(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        log.info("safety.stop_released", path=str(self.path))
        return True


class NoStopSignal:
    """Implements `domain.safety.emergency.EmergencyStop`. Never engaged.

    For the surfaces and the tests where there is nothing to brake.
    """

    def state(self) -> StopState:
        return emergency.RELEASED

    def engaged(self) -> bool:
        return False

    @property
    def reason(self) -> str:
        return ""
