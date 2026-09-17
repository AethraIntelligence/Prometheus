"""Phase 13: one owner per data directory, and a dead owner is proved dead by the kernel."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from domain.safety.ownership import DataDirectoryOwnedError
from infrastructure.runtime.lock import FileRuntimeLock, this_process


def test_a_second_owner_is_refused_and_told_who_owns_it(tmp_path: Path) -> None:
    first = FileRuntimeLock(tmp_path / "data")
    first.acquire(this_process("serve", url="http://127.0.0.1:8765"))

    with pytest.raises(DataDirectoryOwnedError, match="serve") as refused:
        FileRuntimeLock(tmp_path / "data").acquire(this_process("run-task"))

    assert refused.value.owner is not None and refused.value.owner.pid == os.getpid()
    assert FileRuntimeLock(tmp_path / "data").current().url == "http://127.0.0.1:8765"
    first.release()
    assert FileRuntimeLock(tmp_path / "data").current() is None


def test_the_lock_lives_beside_the_data_directory_not_inside_it(tmp_path: Path) -> None:
    lock = FileRuntimeLock(tmp_path / "data")
    lock.acquire(this_process("serve"))
    try:
        assert lock.lock_path.parent == tmp_path.resolve()
        assert not (tmp_path / "data").exists() or not any((tmp_path / "data").iterdir())
    finally:
        lock.release()


_HOLD = """
import sys, time
from pathlib import Path
from infrastructure.runtime.lock import FileRuntimeLock, this_process
lock = FileRuntimeLock(Path(sys.argv[1]))  # held for the life of the process
lock.acquire(this_process("serve"))
print("held", flush=True)
time.sleep(600)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is a POSIX signal")
def test_a_lock_left_by_a_killed_process_is_taken_without_any_guessing(tmp_path: Path) -> None:
    data = tmp_path / "data"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(data)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        # The log line announcing ownership may come first; wait for the marker.
        assert any(line.strip() == "held" for line in iter(holder.stdout.readline, ""))
        with pytest.raises(DataDirectoryOwnedError):
            FileRuntimeLock(data).acquire(this_process("serve"))

        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=30)
        time.sleep(0.05)

        lock = FileRuntimeLock(data)
        lock.acquire(this_process("serve"))
        assert lock.current() is not None and lock.current().pid == os.getpid()
        lock.release()
    finally:
        if holder.poll() is None:
            holder.kill()
