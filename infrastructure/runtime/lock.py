"""The data directory's owner, held by the operating system.

Two files, on purpose. The lock file is locked and never read: on Windows a
locked byte range cannot be read by anybody else, so the record of who holds it
lives beside it, written after the lock is taken and read by whoever failed to
take it.

Both sit *beside* the data directory (`~/.prometheus.runtime.lock` for
`~/.prometheus`), not inside it. A restore replaces the directory with a rename
while its owner holds the lock, and Windows refuses to rename a directory that
has a file open inside it.

The lock is `flock` on macOS and Linux and `msvcrt.locking` on Windows. Both are
released by the kernel when the process ends - exit, crash, `kill -9`, power
loss followed by a boot - which is the whole of stale-lock recovery: whoever can
take the lock has proved that no owner is alive. The owner record a dead process
left behind is simply overwritten.

The descriptor is not inherited across `exec`, so `serve` restarting itself
releases the lock and the new process image takes it again.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import IO

from domain.safety.ownership import DataDirectoryOwnedError, RuntimeOwner
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

LOCK_SUFFIX = ".runtime.lock"
OWNER_SUFFIX = ".runtime.owner.json"


def _try_lock(handle: IO[bytes]) -> bool:
    if sys.platform == "win32":  # pragma: no cover - exercised on the Windows runner
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: no cover - exercised on the Windows runner
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileRuntimeLock:
    """Implements `domain.safety.ownership.RuntimeLock`."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._handle: IO[bytes] | None = None

    @property
    def lock_path(self) -> Path:
        return self._sibling(LOCK_SUFFIX)

    @property
    def owner_path(self) -> Path:
        return self._sibling(OWNER_SUFFIX)

    def _sibling(self, suffix: str) -> Path:
        resolved = self._data_dir.expanduser().resolve()
        return resolved.parent / f"{resolved.name}{suffix}"

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, owner: RuntimeOwner) -> None:
        if self._handle is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        if not _try_lock(handle):
            handle.close()
            raise DataDirectoryOwnedError(self._read_owner())
        self._handle = handle
        temporary = self.owner_path.with_suffix(".tmp")
        temporary.write_text(owner.to_json(), encoding="utf-8")
        os.replace(temporary, self.owner_path)
        log.info("runtime.owned", pid=owner.pid, purpose=owner.purpose)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self.owner_path.unlink(missing_ok=True)
        finally:
            _unlock(self._handle)
            self._handle.close()
            self._handle = None

    def current(self) -> RuntimeOwner | None:
        if self._handle is not None:
            return self._read_owner()
        if not self.lock_path.exists():
            return None
        with self.lock_path.open("a+b") as probe:
            if _try_lock(probe):
                _unlock(probe)
                return None
        return self._read_owner()

    def _read_owner(self) -> RuntimeOwner | None:
        try:
            return RuntimeOwner.from_json(self.owner_path.read_text(encoding="utf-8"))
        except OSError:
            return None


def this_process(purpose: str, *, url: str = "", version: str = "") -> RuntimeOwner:
    from datetime import UTC, datetime

    return RuntimeOwner(
        pid=os.getpid(),
        purpose=purpose,
        host=socket.gethostname(),
        url=url,
        version=version,
        started_at=datetime.now(UTC),
    )
