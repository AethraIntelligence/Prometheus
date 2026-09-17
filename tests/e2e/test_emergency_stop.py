"""Phase 13's emergency stop, through the interface a person uses, and across a restart.

The stack is the real one - FastAPI, the container, SQLite on a temporary file,
the approval gate - with the model scripted. What is proved:

* a task parked on an approval is released with a no when the stop is pulled,
  and the file it wanted to overwrite is untouched;
* while stopped, new work is refused with a reason rather than accepted and
  cancelled;
* a stop set from outside the process - the CLI writing the record - is enforced
  by the running process;
* a runtime started on a stopped machine comes up stopped and resumes nothing;
* resuming allows new work and restarts nothing that was stopped;
* every stop and release is on the audit chain.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.config.settings import Settings
from domain.llm.models import ToolCallRequest
from infrastructure.computer.stop import FileStopSignal
from tests.e2e.test_local_interface import (
    DEADLINE_SECONDS,
    client_for,
    create_schema,
    settings_for,
    wait_for,
)
from tests.fakes.llm import FakeLLM, reply, tool_reply


def _overwrite_script() -> FakeLLM:
    return FakeLLM(
        [
            reply(json.dumps({"steps": [{"description": "Replace the report"}]})),
            tool_reply(
                ToolCallRequest(
                    id="c1",
                    name="fs.write",
                    arguments={"path": "report.md", "content": "the new report"},
                )
            ),
            reply("Done what I could."),
            reply(json.dumps({"passed": True, "reason": "Reported."})),
        ]
    )


def _parked(client, task_id: str) -> list[dict]:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        pending = [
            item
            for item in client.get("/api/approvals").json()["approvals"]
            if item["task_id"] == task_id
        ]
        if pending:
            return pending
        time.sleep(0.02)
    raise AssertionError("the overwrite was never put to a person")


def _audit_actions(settings: Settings) -> list[str]:
    import sqlite3

    with sqlite3.connect(settings.data_dir / "prometheus.db") as connection:
        return [row[0] for row in connection.execute("SELECT action FROM audit_log")]


def _workspace(settings: Settings) -> Path:
    root = settings.data_dir / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.md").write_text("the old report", encoding="utf-8")
    return root


def test_pulling_the_stop_releases_a_parked_approval_and_refuses_new_work(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = _workspace(settings)

    with client_for(settings, _overwrite_script()) as client:
        assert client.get("/api/health").json()["stop"]["engaged"] is False
        task_id = client.post(
            "/api/tasks", json={"goal": "Rewrite the report", "employee": "organizer"}
        ).json()["id"]
        _parked(client, task_id)

        report = client.post("/api/runtime/stop", json={"reason": "wrong folder"}).json()

        assert report["state"]["engaged"] is True
        assert report["state"]["reason"] == "wrong folder"
        assert report["approvals_released"] >= 1
        assert report["completed_effects_undone"] is False
        finished = wait_for(client, task_id, "CANCELLED", "FAILED", "COMPLETED")
        assert finished["status"] == "CANCELLED"
        assert (workspace / "report.md").read_text(encoding="utf-8") == "the old report"

        refused = client.post("/api/tasks", json={"goal": "Another", "employee": "organizer"})
        assert refused.status_code == 409
        assert "stopped" in refused.json()["detail"]
        assert client.get("/api/health").json()["stop"]["reason"] == "wrong folder"

        actions = _audit_actions(settings)
        assert "emergency_stop.engaged" in actions

    # A new process on the same data comes up stopped.
    with client_for(settings, FakeLLM()) as client:
        assert client.get("/api/runtime/stop").json()["engaged"] is True
        assert client.post(
            "/api/tasks", json={"goal": "After restart", "employee": "organizer"}
        ).status_code == 409

        released = client.post("/api/runtime/resume").json()
        assert released["state"]["engaged"] is False
        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "CANCELLED", (
            "resuming work restarts nothing that was stopped"
        )
        actions = _audit_actions(settings)
        assert "emergency_stop.released" in actions


def test_a_stop_set_from_another_terminal_is_enforced_by_the_running_process(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = _workspace(settings)

    with client_for(settings, _overwrite_script()) as client:
        task_id = client.post(
            "/api/tasks", json={"goal": "Rewrite the report", "employee": "organizer"}
        ).json()["id"]
        _parked(client, task_id)

        # What `prometheus stop` does: write the record, reach no memory.
        FileStopSignal(settings.stop_file_path).engage("from the terminal", by="cli")

        finished = wait_for(client, task_id, "CANCELLED", "FAILED", "COMPLETED")
        assert finished["status"] == "CANCELLED"
        assert (workspace / "report.md").read_text(encoding="utf-8") == "the old report"
        assert client.get("/api/approvals").json()["approvals"] == []


def test_work_left_by_a_crash_is_not_resumed_on_a_stopped_machine(tmp_path: Path) -> None:
    import asyncio

    from domain.tasks.task import Task, TaskStatus
    from infrastructure.persistence.session import create_engine, create_session_factory
    from infrastructure.persistence.task_repository import SqlTaskRepository

    settings = settings_for(tmp_path)
    create_schema(settings)

    async def _leave_behind() -> str:
        engine = create_engine(settings.resolved_database_url)
        repository = SqlTaskRepository(create_session_factory(engine))
        task = Task.create("Interrupted by a crash")
        await repository.save(task)
        running, event = task.transition_to(TaskStatus.RUNNING)
        await repository.save(running, event)
        await engine.dispose()
        return str(task.id)

    task_id = asyncio.run(_leave_behind())
    FileStopSignal(settings.stop_file_path).engage("stopped before the crash")

    with client_for(settings, FakeLLM()) as client:
        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "CANCELLED"
        actions = _audit_actions(settings)
        assert "emergency_stop.enforced" in actions
