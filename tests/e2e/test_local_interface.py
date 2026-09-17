"""Phase 6's Definition of Done: a task is run and finished from the interface.

The whole stack is real - FastAPI, the container, SQLite on a temporary file,
the employee runtime, the approval gate - with two things scripted: the model,
because the suite runs without a provider key, and the employee declaration,
which is read from the repository's own `employees/` directory.

The interface is exercised the way a browser exercises it: post a goal, watch
the stream, open the history, answer a question, stop a run.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from domain.llm.models import ToolCallRequest
from infrastructure.persistence.models import Base
from infrastructure.persistence.session import create_engine
from tests.fakes.llm import FakeLLM, reply, tool_reply

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Long enough for a scripted run to finish on a loaded machine, short enough
#: that a genuinely stuck run fails the test rather than hanging it.
DEADLINE_SECONDS = 10.0


class SlowLLM(FakeLLM):
    """A scripted model that takes long enough to be interrupted.

    Every other test wants the run over instantly. A test about stopping one
    needs a run that is still going when the request to stop it arrives, and a
    small await is a truer stand-in for a provider call than a sleep that would
    block the loop the server is answering on.
    """

    async def generate(self, request):  # type: ignore[override]
        await asyncio.sleep(0.05)
        return await super().generate(request)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}",
        file_root=tmp_path / "workspace",
        employees_dir=REPO_ROOT / "employees",
        ui_approval_timeout_seconds=5.0,
        log_format="console",
    )


def create_schema(settings: Settings) -> None:
    import asyncio

    async def _create() -> None:
        engine = create_engine(settings.resolved_database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())


def client_for(settings: Settings, llm: FakeLLM) -> TestClient:
    """The real application, with every model call answered from a script."""

    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: llm  # type: ignore[method-assign]
        return container

    return TestClient(create_app(settings, build=build))


def wait_for(client: TestClient, task_id: str, *statuses: str) -> dict:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in statuses:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task stayed {task['status']}, wanted one of {statuses}")


# --- The page it draws ---------------------------------------------------------


def test_the_page_and_its_assets_are_served(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(settings, FakeLLM()) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Prometheus" in page.text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/app.css").status_code == 200


def test_the_declared_employees_are_offered(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(settings, FakeLLM()) as client:
        names = {e["name"] for e in client.get("/api/employees").json()["employees"]}
        assert "researcher" in names, "the interface offers what is declared, not a hard-coded list"


# --- A task, start to finish ---------------------------------------------------


def test_a_task_is_started_watched_and_finished_from_the_interface(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    # Plan, execute, verify: three scripted answers is one whole run.
    llm = FakeLLM(
        [
            reply(json.dumps({"steps": [{"description": "Say it"}], "rationale": "Short."})),
            reply("SQLite keeps the whole database in one file."),
            reply(json.dumps({"passed": True, "reason": "The goal is answered."})),
        ]
    )

    with client_for(settings, llm) as client:
        created = client.post(
            "/api/tasks", json={"goal": "Explain SQLite in a line", "employee": "researcher"}
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["running"] is True

        finished = wait_for(client, task_id, "COMPLETED", "FAILED", "CANCELLED")
        assert finished["status"] == "COMPLETED", finished.get("error")
        assert "SQLite" in finished["result"]["summary"]
        assert finished["employee"] == "researcher"
        assert finished["plan"]["steps"][0]["description"] == "Say it"

        # 6.5: what it cost, on screen. The scripted model is free and is not
        # wrapped in the metering client, so what is asserted here is that the
        # interface reports spend at all - what it adds up is Phase 2's test.
        assert finished["cost_usd"] == 0.0
        assert set(client.get("/api/spend").json()) == {
            "calls",
            "prompt_tokens",
            "output_tokens",
            "cost_usd",
        }

        # 6.7: and it is in the history, openable afterwards.
        history = client.get("/api/tasks").json()["tasks"]
        assert [t["id"] for t in history] == [task_id]
        assert history[0]["running"] is False


def test_a_failure_is_reported_in_the_interface_rather_than_the_terminal(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    from tests.fakes.llm import transient

    # Every attempt fails the same way, which is what makes it terminal.
    llm = FakeLLM([transient("provider is down") for _ in range(8)])

    with client_for(settings, llm) as client:
        task_id = client.post(
            "/api/tasks", json={"goal": "Answer something", "employee": "researcher"}
        ).json()["id"]

        failed = wait_for(client, task_id, "FAILED", "COMPLETED")
        assert failed["status"] == "FAILED"
        assert failed["error"]["message"]


def test_asking_for_an_employee_that_does_not_exist_is_answered_not_crashed(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(settings, FakeLLM()) as client:
        refused = client.post("/api/tasks", json={"goal": "Do it", "employee": "nobody"})
        assert refused.status_code == 400
        assert "nobody" in refused.json()["detail"]


def test_an_unknown_task_is_a_404_not_a_stack_trace(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(settings, FakeLLM()) as client:
        assert client.get("/api/tasks/00000000-0000-0000-0000-000000000001").status_code == 404


# --- The trace ------------------------------------------------------------------


def test_the_trace_names_the_tool_the_arguments_and_the_interface(tmp_path: Path) -> None:
    """6.3: the step, the tool, its arguments and what came back, as it happens."""
    settings = settings_for(tmp_path)
    create_schema(settings)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "notes.txt").write_text("the answer is 41", encoding="utf-8")

    llm = FakeLLM(
        [
            reply(json.dumps({"steps": [{"description": "Read the notes"}]})),
            tool_reply(
                ToolCallRequest(id="c1", name="fs.read", arguments={"path": "notes.txt"})
            ),
            reply("The notes say the answer is 41."),
            reply(json.dumps({"passed": True, "reason": "Read and reported."})),
        ]
    )

    with client_for(settings, llm) as client:
        task_id = client.post(
            "/api/tasks", json={"goal": "What do the notes say?", "employee": "researcher"}
        ).json()["id"]
        finished = wait_for(client, task_id, "COMPLETED", "FAILED")
        assert finished["status"] == "COMPLETED", finished.get("error")

        calls = finished["calls"]
        assert [call["tool"] for call in calls] == ["fs.read"]
        assert calls[0]["arguments"] == {"path": "notes.txt"}
        assert calls[0]["interface"] == "API"
        assert calls[0]["success"] is True
        assert finished["events"][0]["to"] == "PLANNING"


def test_the_stream_replays_what_a_task_already_said(tmp_path: Path) -> None:
    """A page opened after a run has to show the lines it missed, not start blank.

    The stream is read to the end here, which is only possible because a stream
    watching one task ends when that task does - a run with nothing left to say
    closes the connection instead of holding it open.
    """
    settings = settings_for(tmp_path)
    create_schema(settings)
    llm = FakeLLM(
        [
            reply(json.dumps({"steps": [{"description": "Answer"}], "rationale": "Direct."})),
            reply("Forty-one."),
            reply(json.dumps({"passed": True, "reason": "Answered."})),
        ]
    )

    with client_for(settings, llm) as client:
        task_id = client.post(
            "/api/tasks", json={"goal": "How many?", "employee": "researcher"}
        ).json()["id"]
        wait_for(client, task_id, "COMPLETED", "FAILED")

        replayed = client.get(f"/api/events?task={task_id}")
        assert replayed.status_code == 200
        assert replayed.headers["content-type"].startswith("text/event-stream")
        lines = [
            json.loads(raw[6:])
            for raw in replayed.text.splitlines()
            if raw.startswith("data: ")
        ]

        kinds = [item["kind"] for item in lines]
        assert kinds[0] == "STAGE", "who it went to comes first"
        assert "PLAN" in kinds
        assert kinds[-1] == "RESULT"
        assert all(item["task_id"] == task_id for item in lines)


# --- Stopping -------------------------------------------------------------------


def test_a_running_task_can_be_stopped_from_the_interface(tmp_path: Path) -> None:
    """6.6: and what it had done by then is kept, not thrown away."""
    settings = settings_for(tmp_path)
    create_schema(settings)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    # The model would keep listing the folder forever; the stop is what ends it.
    llm = SlowLLM(
        [reply(json.dumps({"steps": [{"description": "Loop"}]}))]
        + [
            tool_reply(ToolCallRequest(id=f"c{i}", name="fs.list", arguments={"path": "."}))
            for i in range(50)
        ]
    )

    with client_for(settings, llm) as client:
        task_id = client.post(
            "/api/tasks", json={"goal": "Keep looking", "employee": "organizer"}
        ).json()["id"]

        # Stop it once it has really done something, so what is under test is a
        # run being interrupted rather than one being refused before it started.
        deadline = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            if client.get(f"/api/tasks/{task_id}").json()["calls"]:
                break
            time.sleep(0.01)

        stopped = client.post(f"/api/tasks/{task_id}/cancel", json={"reason": "enough"})
        assert stopped.status_code == 200

        final = wait_for(client, task_id, "CANCELLED", "COMPLETED", "FAILED")
        assert final["status"] == "CANCELLED"
        assert final["calls"], "the work done before the stop is still recorded"
        assert len(final["calls"]) < 50, "it stopped rather than running to the end"


def test_cancelling_a_task_left_behind_by_an_earlier_process(tmp_path: Path) -> None:
    """Nothing in this process is running it, so nothing can be asked to stop."""
    settings = settings_for(tmp_path)
    create_schema(settings)

    import asyncio

    from domain.tasks.task import Task, TaskStatus
    from infrastructure.persistence.session import create_session_factory
    from infrastructure.persistence.task_repository import SqlTaskRepository

    async def _leave_behind() -> str:
        engine = create_engine(settings.resolved_database_url)
        repository = SqlTaskRepository(create_session_factory(engine))
        task = Task.create("Interrupted last time")
        await repository.save(task)
        running, event = task.transition_to(TaskStatus.RUNNING)
        await repository.save(running, event)
        await engine.dispose()
        return str(task.id)

    task_id = asyncio.run(_leave_behind())

    with client_for(settings, FakeLLM()) as client:
        stopped = client.post(f"/api/tasks/{task_id}/cancel", json={"reason": "stale"})
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "CANCELLED"


def test_cancelling_something_that_does_not_exist_is_a_404(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(settings, FakeLLM()) as client:
        missing = client.post(
            "/api/tasks/00000000-0000-0000-0000-000000000002/cancel", json={"reason": ""}
        )
        assert missing.status_code == 404


# --- Approvals ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("approved", "expected"),
    [(True, "written"), (False, "did not approve")],
)
def test_an_irreversible_action_waits_for_the_interface(
    tmp_path: Path, approved: bool, expected: str
) -> None:
    """6.4: the run really is parked, and the button is what releases it."""
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "report.md").write_text("the old report", encoding="utf-8")

    llm = FakeLLM(
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

    with client_for(settings, llm) as client:
        task_id = client.post(
            "/api/tasks", json={"goal": "Rewrite the report", "employee": "organizer"}
        ).json()["id"]

        deadline = time.monotonic() + DEADLINE_SECONDS
        pending: list[dict] = []
        while time.monotonic() < deadline and not pending:
            pending = client.get("/api/approvals").json()["approvals"]
            time.sleep(0.02)

        assert pending, "an overwrite must not proceed without being asked about"
        assert pending[0]["task_id"] == task_id
        assert pending[0]["live"] is True
        assert pending[0]["risk"] in ("HIGH", "CRITICAL")
        assert pending[0]["scope"]["action"] == "fs.write"
        assert pending[0]["scope"]["resource"] == "path:report.md"
        assert pending[0]["preview"]["kind"] == "file_diff"
        assert "-the old report" in pending[0]["preview"]["diff"]
        assert "+the new report" in pending[0]["preview"]["diff"]
        assert (workspace / "report.md").read_text(encoding="utf-8") == "the old report"

        decision = {"approved": approved, "comment": ""}
        if approved:
            decision.update({"grant": "TASK", "duration_seconds": 3600})
        answered = client.post(f"/api/approvals/{pending[0]['id']}", json=decision)
        assert answered.status_code == 200
        assert answered.json()["live"] is True
        if approved:
            [lease] = client.get("/api/capability-leases").json()["leases"]
            assert lease["action"] == "fs.write"
            assert lease["resource"] == "path:report.md"
            assert lease["task_id"] == task_id
            revoked = client.delete(f"/api/capability-leases/{lease['id']}")
            assert revoked.status_code == 200
            assert client.get("/api/capability-leases").json()["leases"] == []

        finished = wait_for(client, task_id, "COMPLETED", "FAILED", "CANCELLED")
        del finished
        calls = client.get(f"/api/tasks/{task_id}").json()["calls"]
        outcome = (workspace / "report.md").read_text(encoding="utf-8")

        if approved:
            assert outcome == "the new report"
            assert calls[0]["success"] is True
        else:
            assert outcome == "the old report"
            assert expected in (calls[0]["error"] or ""), calls[0]
