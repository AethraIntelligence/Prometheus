"""Phase 13's release drill, against a real `prometheus serve` process.

Everything else in the suite runs the application inside the test process. This
file starts the runtime the way the desktop shell does - a child process, its
own data directory, a real port - because what it proves only exists between
processes: one owner per data directory, a stop written by a second process and
enforced by the first, a backup taken while the engine runs, and a restore that
happens between one process image and the next.

No model is needed and none is called. The packaged installer's half of the
drill runs on the release workflow's machines (`.github/workflows/release.yml`).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The installed console script, as a shell starts it: a restart re-executes
#: exactly what was started, so `python -m` would restart as a bare file.
PROMETHEUS = Path(sys.executable).parent / ("prometheus.exe" if os.name == "nt" else "prometheus")
BOOT_SECONDS = 60.0

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process control")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _environment(data_dir: Path, port: int) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PROMETHEUS_")
    }
    environment.update(
        {
            "PROMETHEUS_DATA_DIR": str(data_dir),
            "PROMETHEUS_UI_PORT": str(port),
            "PROMETHEUS_SECRET_BACKEND": "file",
            "PROMETHEUS_LOCAL_LLM_AUTOSTART": "false",
            "PROMETHEUS_FLAGS__BROWSER_TOOLS": "false",
            "PROMETHEUS_LOG_FORMAT": "console",
        }
    )
    return environment


def _prometheus(*args: str, environment: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PROMETHEUS), *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        **kwargs,
    )


def _call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _health(port: int, *, not_started_at: str = "") -> dict:
    deadline = time.monotonic() + BOOT_SECONDS
    while time.monotonic() < deadline:
        try:
            status, body = _call(port, "GET", "/api/health")
            if status == 200 and body.get("started_at") != not_started_at:
                return body
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.2)
    raise AssertionError("the runtime did not answer")


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[subprocess.Popen, dict[str, str], int, Path]]:
    if not hasattr(os, "execv"):
        pytest.skip("the runtime restarts itself with execv")
    data_dir = tmp_path / "data"
    port = _free_port()
    environment = _environment(data_dir, port)
    process = subprocess.Popen(
        [str(PROMETHEUS), "serve"],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _health(port)
        yield process, environment, port, data_dir
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


def test_the_drill_between_processes(runtime, tmp_path: Path) -> None:
    process, environment, port, data_dir = runtime

    # A fresh installation was created at the head on first start.
    database = data_dir / "prometheus.db"
    assert database.exists()

    # One owner: a second engine on the same data refuses and names the first.
    second = _prometheus("serve", environment={**environment, "PROMETHEUS_UI_PORT": "1"})
    assert second.returncode == 3, second.stderr
    assert f"pid {process.pid}" in second.stderr
    assert _prometheus("run-task", "--employee", "organizer", "x", environment=environment)\
        .returncode == 3

    # A thread exists, then a backup is taken while the engine runs.
    status, thread = _call(port, "POST", "/api/conversations", {"title": "Before the backup"})
    assert status == 201
    archive = tmp_path / "drill.zip"
    status, made = _call(port, "POST", "/api/backups", {"destination": str(archive)})
    assert status == 201, made
    assert archive.exists() and made["includes_secrets"] is False

    # Something changes after the backup, which the restore must undo.
    status, _ = _call(port, "POST", "/api/conversations", {"title": "After the backup"})
    assert status == 201

    before = _health(port)
    status, accepted = _call(port, "POST", "/api/runtime/restore", {"path": str(archive)})
    assert status == 202, accepted
    after = _health(port, not_started_at=before["started_at"])
    assert after["status"] == "ok"
    assert process.poll() is None, "the same process, replaced in place"

    status, listed = _call(port, "GET", "/api/conversations")
    titles = [item["title"] for item in listed["conversations"]]
    assert titles == ["Before the backup"], titles
    kept = [path for path in data_dir.parent.iterdir() if ".pre-restore-" in path.name]
    assert len(kept) == 1, "the replaced installation is kept"

    # A stop from another terminal reaches the running engine and survives it.
    stopped = _prometheus("stop", "--reason", "drill", environment=environment)
    assert stopped.returncode == 0, stopped.stderr
    assert _health(port)["stop"]["engaged"] is True
    status, refused = _call(port, "POST", "/api/conversations/" + thread["id"] + "/messages",
                            {"request": "anything"})
    assert status == 409, refused
    resumed = _prometheus("stop", "--clear", environment=environment)
    assert resumed.returncode == 0

    with sqlite3.connect(database) as connection:
        actions = {row[0] for row in connection.execute("SELECT action FROM audit_log")}
    assert {"emergency_stop.engaged", "emergency_stop.released"} <= actions
    verified = _prometheus("audit", "--verify", environment=environment)
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_a_corrupted_backup_is_refused_and_the_engine_keeps_running(runtime, tmp_path) -> None:
    process, _, port, _ = runtime
    archive = tmp_path / "broken.zip"
    status, _ = _call(port, "POST", "/api/backups", {"destination": str(archive)})
    assert status == 201
    damaged = bytearray(archive.read_bytes())
    damaged[len(damaged) // 3] ^= 0xFF
    archive.write_bytes(bytes(damaged))
    started = _health(port)["started_at"]

    status, refused = _call(port, "POST", "/api/runtime/restore", {"path": str(archive)})

    assert status == 400, refused
    time.sleep(1.0)
    assert process.poll() is None
    assert _health(port)["started_at"] == started, "no restart for a backup that failed its check"
