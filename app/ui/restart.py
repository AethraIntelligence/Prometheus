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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class _Stoppable(Protocol):
    should_exit: bool


@dataclass(frozen=True, slots=True)
class PendingRestore:
    """A verified backup to put in place between this process and the next."""

    archive: Path
    passphrase: str | None = field(default=None, repr=False)
    skip_secrets: bool = False


class RestartSignal:
    """One per process: whether a restart is possible, and whether one was asked for."""

    def __init__(self) -> None:
        self._server: _Stoppable | None = None
        self.requested = False
        #: Set by a restore request. A restore replaces the store this process
        #: has open, so it runs after the server has stopped and before the
        #: process replaces itself - never while anything could write.
        self.restore: PendingRestore | None = None
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
        """Run the same command again in this process, with everything reread.

        `sys.orig_argv` rather than `sys.argv`: the packaged application starts
        the runtime as `python -m app.cli.main serve`, and `sys.argv` has lost
        the `-m` by then - re-executing it would run the module as a bare file,
        outside its package.
        """
        arguments = [sys.executable, *sys.orig_argv[1:]]
        log.info("runtime.restarting", argv=arguments)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, arguments)


#: The process's signal. Module-level because the app is built by uvicorn's
#: factory call and the server by `serve`, and this is where the two meet.
RESTART = RestartSignal()
