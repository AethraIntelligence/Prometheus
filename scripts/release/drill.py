"""The Phase 13 release drill, against whatever starts the runtime.

On a release runner `--launch` is the installed application's own binary: the
shell starts its bundled runtime, and the drill talks to it over the same
loopback HTTP the window does. On a developer's machine it is
`uv run prometheus serve`. The sequence is the same either way, and each step
records what it saw into an evidence file the workflow keeps:

  first run -> model -> a task with an approval and an artifact -> graceful
  restart -> forced crash and recovery -> (update at a safe point, when asked)
  -> backup -> delete the local data -> restore -> the result is there again
  -> emergency stop

A step that fails stops the drill and the evidence says which one and why.
No provider key is used: `scripted_model.py` answers on loopback.

    uv run python scripts/release/drill.py --launch "uv run prometheus serve" \\
        --evidence drill-evidence.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import scripted_model  # noqa: E402

BOOT_SECONDS = 180.0
STEP_SECONDS = 60.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Drill:
    def __init__(self, launch: str, root: Path, evidence: Path) -> None:
        self.launch = launch
        self.root = root
        self.data_dir = root / "data"
        self.files = self.data_dir / "workspace"
        self.port = free_port()
        self.model_port = free_port()
        self.evidence_path = evidence
        self.evidence: dict = {
            "launch": launch,
            "platform": sys.platform,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "steps": [],
        }
        self.process: subprocess.Popen | None = None
        self.task_id = ""

    # --- The runtime ------------------------------------------------------------------

    def environment(self) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("PROMETHEUS_")
        }
        environment.update(
            {
                "PROMETHEUS_DATA_DIR": str(self.data_dir),
                "PROMETHEUS_UI_PORT": str(self.port),
                "PROMETHEUS_BASE_URL": f"http://127.0.0.1:{self.port}",
                "PROMETHEUS_MODEL_CATALOG_PATH": str(HERE / "drill-models.toml"),
                "PROMETHEUS_LOCAL_LLM_BASE_URL": f"http://127.0.0.1:{self.model_port}/v1",
                "PROMETHEUS_LOCAL_LLM_AUTOSTART": "false",
                "PROMETHEUS_SECRET_BACKEND": os.environ.get("PROMETHEUS_SECRET_BACKEND", "file"),
                "PROMETHEUS_FLAGS__BROWSER_TOOLS": "false",
                "PROMETHEUS_FLAGS__MEMORY": "false",
                "PROMETHEUS_FLAGS__KNOWLEDGE": "false",
                "PROMETHEUS_LOG_FORMAT": "console",
            }
        )
        return environment

    def start(self) -> dict:
        log = (self.root / "runtime.log").open("ab")
        self.process = subprocess.Popen(
            shlex.split(self.launch, posix=os.name != "nt"),
            env=self.environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        return self.health(timeout=BOOT_SECONDS)

    def stop(self) -> None:
        if self.process is None:
            return
        if os.name != "nt":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.process.pid, signal.SIGTERM)
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None
        # An application shell starts the runtime in a process group of its
        # own, so a signal to the shell alone can leave the runtime serving.
        # The owner record names it; a stop that did not reach it asks it too.
        with contextlib.suppress(OSError, ValueError, KeyError):
            pid = self.owner_pid()
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid)], check=False)
            else:
                os.kill(pid, signal.SIGTERM)
        self.wait_until_down()

    def owner_pid(self) -> int:
        owner = self.data_dir.parent / f"{self.data_dir.name}.runtime.owner.json"
        return int(json.loads(owner.read_text(encoding="utf-8"))["pid"])

    def crash(self) -> int:
        """SIGKILL the runtime itself - not the shell - exactly as a power cut would."""
        pid = self.owner_pid()
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=False)
        else:
            os.kill(pid, signal.SIGKILL)
        self.wait_until_down()
        if self.process is not None:
            if os.name != "nt":
                # macOS answers EPERM, not ESRCH, for a group whose leader is dead.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.process.pid, signal.SIGKILL)
            else:
                self.process.kill()
            self.process.wait(timeout=30)
            self.process = None
        return pid

    def wait_until_down(self) -> None:
        deadline = time.monotonic() + STEP_SECONDS
        while time.monotonic() < deadline:
            try:
                self.call("GET", "/api/health")
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                return
            time.sleep(0.3)
        raise AssertionError("the runtime did not stop")

    # --- Talking to it ------------------------------------------------------------------

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def health(self, *, timeout: float = STEP_SECONDS, not_started_at: str = "") -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status, body = self.call("GET", "/api/health")
                if status == 200 and body.get("started_at") != not_started_at:
                    return body
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                pass
            time.sleep(0.5)
        raise AssertionError(f"the runtime did not answer within {timeout:.0f}s")

    def until(self, what: str, check: Callable[[], object], timeout: float = STEP_SECONDS):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = check()
            if found:
                return found
            time.sleep(0.3)
        raise AssertionError(f"timed out waiting for {what}")

    def task(self, task_id: str) -> dict:
        return self.call("GET", f"/api/tasks/{task_id}")[1]

    def pending_for(self, task_id: str) -> list[dict]:
        approvals = self.call("GET", "/api/approvals")[1].get("approvals", [])
        return [item for item in approvals if item.get("task_id") == task_id]

    # --- The steps ----------------------------------------------------------------------

    def step(self, name: str, action: Callable[[], dict]) -> None:
        started = time.monotonic()
        record: dict = {"step": name}
        try:
            record["observed"] = action() or {}
            record["result"] = "passed"
        except BaseException as error:
            record["result"] = "failed"
            record["error"] = f"{type(error).__name__}: {error}"
            record["trace"] = traceback.format_exc(limit=5)
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 2)
            self.evidence["steps"].append(record)
            self.write()
        print(f"  {name}: {record['result']} ({record['seconds']}s)", flush=True)

    def write(self) -> None:
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")

    def first_run(self) -> dict:
        health = self.start()
        assert health["status"] == "ok"
        assert (self.data_dir / "prometheus.db").exists(), "the database was created"
        assert health["stop"]["engaged"] is False
        return {"health": health}

    def model(self) -> dict:
        status, providers = self.call("GET", "/api/providers")
        return {"status": status, "catalog": providers if status == 200 else None}

    def task_with_approval_and_artifact(self) -> dict:
        self.files.mkdir(parents=True, exist_ok=True)
        (self.files / scripted_model.TARGET).write_text("the old report", encoding="utf-8")
        status, created = self.call(
            "POST", "/api/tasks", {"goal": "Rewrite report.md", "employee": "organizer"}
        )
        assert status == 201, created
        self.task_id = created["id"]
        [question] = self.until("the overwrite to be put to a person",
                                lambda: self.pending_for(self.task_id))
        status, decided = self.call(
            "POST", f"/api/approvals/{question['id']}", {"approved": True, "comment": "drill"}
        )
        assert status == 200, decided
        finished = self.until(
            "the task to finish",
            lambda: (t := self.task(self.task_id))["status"] in ("COMPLETED", "FAILED") and t,
        )
        assert finished["status"] == "COMPLETED", finished
        written = (self.files / scripted_model.TARGET).read_text(encoding="utf-8")
        assert written == scripted_model.CONTENT
        return {"task": self.task_id, "approval": question["id"], "artifact": scripted_model.TARGET}

    def graceful_restart(self) -> dict:
        before = self.health()
        if not before.get("can_restart"):
            return {"skipped": "this launch cannot restart itself"}
        status, _ = self.call("POST", "/api/runtime/restart")
        assert status == 202
        after = self.health(timeout=BOOT_SECONDS, not_started_at=before["started_at"])
        assert self.task(self.task_id)["status"] == "COMPLETED"
        return {"before": before["started_at"], "after": after["started_at"]}

    def forced_crash_and_recovery(self) -> dict:
        (self.files / "second.md").write_text("keep me", encoding="utf-8")
        status, created = self.call(
            "POST", "/api/tasks", {"goal": "Rewrite report.md again", "employee": "organizer"}
        )
        assert status == 201, created
        parked = created["id"]
        self.until("the second overwrite to wait", lambda: self.pending_for(parked))
        killed = self.crash()
        self.start()
        task = self.task(parked)
        # Nobody can answer a question the dead process asked: it is expired,
        # the task is back where resuming reaches it, and the overwrite it was
        # asking about did not happen.
        assert not self.pending_for(parked), "no live question survives the crash"
        assert task["status"] == "RUNNING", task
        assert (self.files / scripted_model.TARGET).read_text(encoding="utf-8") == (
            scripted_model.CONTENT
        )
        status, verified = self.call("GET", "/api/audit/verify")
        assert status == 200 and verified.get("valid", verified.get("ok", True)), verified
        assert self.task(self.task_id)["status"] == "COMPLETED"
        return {"killed_pid": killed, "parked_task": parked, "status_after": task["status"]}

    def backup(self) -> dict:
        destination = self.root / "evidence-backup.zip"
        status, made = self.call("POST", "/api/backups", {"destination": str(destination)})
        assert status == 201, made
        return made

    def delete_local_data(self) -> dict:
        self.stop()
        shutil.rmtree(self.data_dir)
        for sibling in self.data_dir.parent.glob(f"{self.data_dir.name}.runtime.*"):
            sibling.unlink(missing_ok=True)
        assert not self.data_dir.exists()
        health = self.start()
        assert self.call("GET", f"/api/tasks/{self.task_id}")[0] == 404
        return {"fresh": health["started_at"]}

    def restore(self) -> dict:
        before = self.health()
        status, accepted = self.call(
            "POST", "/api/runtime/restore", {"path": str(self.root / "evidence-backup.zip")}
        )
        assert status == 202, accepted
        after = self.health(timeout=BOOT_SECONDS, not_started_at=before["started_at"])
        return {"accepted": accepted["backup"], "after": after["started_at"]}

    def result_again(self) -> dict:
        task = self.task(self.task_id)
        assert task["status"] == "COMPLETED", task
        written = (self.files / scripted_model.TARGET).read_text(encoding="utf-8")
        assert written == scripted_model.CONTENT
        return {"task": task["status"], "artifact": "restored"}

    def emergency_stop(self) -> dict:
        status, report = self.call("POST", "/api/runtime/stop", {"reason": "release drill"})
        assert status == 200 and report["state"]["engaged"], report
        assert report["tasks_closed"] >= 1, "the task the crash left is closed, not resumed later"
        refused, body = self.call(
            "POST", "/api/tasks", {"goal": "Anything", "employee": "organizer"}
        )
        assert refused == 409, body
        restarted_state = self.health()["stop"]
        assert restarted_state["engaged"]
        status, resumed = self.call("POST", "/api/runtime/resume")
        assert status == 200 and not resumed["state"]["engaged"]
        status, verified = self.call("GET", "/api/audit/verify")
        assert status == 200, verified
        return {"report": report, "audit": verified}

    def run(self, update: Callable[[Drill], dict] | None) -> None:
        model = scripted_model.serve(self.model_port)
        try:
            self.step("first_run", self.first_run)
            self.step("model", self.model)
            self.step("task_with_approval_and_artifact", self.task_with_approval_and_artifact)
            self.step("graceful_restart", self.graceful_restart)
            self.step("forced_crash_and_recovery", self.forced_crash_and_recovery)
            if update is not None:
                self.step("update_at_safe_point", lambda: update(self))
            else:
                self.evidence["steps"].append(
                    {"step": "update_at_safe_point", "result": "skipped",
                     "reason": "no --update-launch given; run on a release runner"}
                )
            self.step("backup", self.backup)
            self.step("delete_local_data", self.delete_local_data)
            self.step("restore", self.restore)
            self.step("result_again", self.result_again)
            self.step("emergency_stop", self.emergency_stop)
            self.evidence["result"] = "passed"
        except BaseException:
            self.evidence["result"] = "failed"
            raise
        finally:
            self.evidence["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.write()
            with contextlib.suppress(Exception):
                self.stop()
            model.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--launch", required=True, help="The command that starts the runtime.")
    parser.add_argument("--evidence", type=Path, default=Path("drill-evidence.json"))
    parser.add_argument(
        "--update-launch",
        help="The newer build's command; the drill installs it over the running one.",
    )
    parser.add_argument("--keep", action="store_true", help="Keep the drill's data directory.")
    arguments = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="prometheus-drill-"))
    drill = Drill(arguments.launch, root, arguments.evidence.resolve())
    update = None
    if arguments.update_launch:
        def update(current: Drill) -> dict:
            before = current.health()
            status, prepared = current.call(
                "POST", "/api/runtime/prepare-update", {"timeout_seconds": 60}
            )
            assert status == 200 and prepared["ready"], prepared
            current.stop()
            current.launch = arguments.update_launch
            after = current.start()
            assert current.task(current.task_id)["status"] == "COMPLETED"
            return {"before": before.get("started_at"), "after": after.get("started_at")}

    print(f"Release drill in {root}")
    try:
        drill.run(update)
    except BaseException as error:
        print(f"FAILED: {error}", file=sys.stderr)
        print(f"runtime log: {root / 'runtime.log'}", file=sys.stderr)
        return 1
    finally:
        if not arguments.keep and drill.evidence.get("result") == "passed":
            shutil.rmtree(root, ignore_errors=True)
    print(f"Passed. Evidence: {arguments.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
