"""Restarting the runtime from the window, whoever started it.

A runtime reads its code, its `.env` and Settings -> General once, when it
starts. Until now the only way to apply any of those was to find the process and
kill it - and a desktop window reattaches to whatever is already answering on
the port, so closing and reopening the window changed nothing. A person saw a
fix that was on disk and not in memory, and a switch marked "after restart"
with nothing to press.

**The process restarts itself.** The shell owns only a runtime it started, and a
runtime started in a terminal is not the shell's to kill. So the runtime stops
the way it stops on Ctrl+C - the server stops taking requests, live runs are
asked to stop cooperatively and write their own state, the scheduler finishes
its tick - and then replaces itself with a fresh interpreter running the same
command (`os.execv`). Same pid, same terminal, same parent: a shell holding it
as a child still holds it, and nothing else on the machine has to learn it moved.

**Only `prometheus serve` can do this**, because only it owns the server loop
that has to stop first. An app built for a test, or served with `--reload`
(whose reloader already restarts on its own), reports that it cannot.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class _Stoppable(Protocol):
    should_exit: bool


class RestartSignal:
    """One per process: whether a restart is possible, and whether one was asked for."""

    def __init__(self) -> None:
        self._server: _Stoppable | None = None
        self.requested = False
        #: When this interpreter started. A window waiting for a restart knows
        #: the new process is up when this changes.
        self.started_at = datetime.now(UTC).isoformat()

    @property
    def available(self) -> bool:
        return self._server is not None

    def attach(self, server: _Stoppable) -> None:
        self._server = server

    def request(self) -> None:
        """Stop serving, gracefully; `serve` replaces the process once it has."""
        if self._server is None:
            return
        log.info("runtime.restart_requested")
        self.requested = True
        self._server.should_exit = True

    def replace_process(self) -> None:  # pragma: no cover - replaces the test runner
        """Run the same command again in this process, with everything reread."""
        log.info("runtime.restarting", argv=sys.argv)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, *sys.argv])


#: The process's signal. Module-level because the app is built by uvicorn's
#: factory call and the server by `serve`, and this is where the two meet.
RESTART = RestartSignal()
