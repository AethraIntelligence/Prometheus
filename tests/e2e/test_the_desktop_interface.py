"""Phase 13's Definition of Done: a conversation, through the boundary a shell uses.

The desktop window talks HTTP and server-sent events to the local runtime, so
this exercises exactly that: the FastAPI adapter, the application-level
boundary, the manager, the employee runtime, the approval gate and SQLite on a
temporary file. One thing is scripted - the model - because the suite runs
without a provider key.

What is being proved is not "the endpoints answer". It is that the interface
layer decides nothing: the same sentence produces the same work whatever it says
about where it came from, the answer a window shows is the one the store holds,
and an irreversible action still waits for a person who is not on the call stack
that asked.
"""

from __future__ import annotations

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
DEADLINE_SECONDS = 10.0


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


def script(*answers) -> FakeLLM:
    return FakeLLM([reply(item) if isinstance(item, str) else item for item in answers])


def client_for(settings: Settings, llm: FakeLLM) -> TestClient:
    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: llm  # type: ignore[method-assign]
        return container

    return TestClient(create_app(settings, build=build))


def intent(**overrides) -> str:
    return json.dumps(
        {
            "restatement": "read the notes and say what they contain",
            "constraints": {},
            "acceptance_criteria": ["the notes are summarised"],
            "needs_work": True,
            "answer": "",
            **overrides,
        }
    )


def plan(*goals: str) -> str:
    return json.dumps(
        {
            "rationale": "one step is enough",
            "tasks": [{"id": f"t{i}", "goal": goal} for i, goal in enumerate(goals)],
        }
    )


def chooses(name: str) -> str:
    return json.dumps({"employee": name, "reason": "it has the tools", "facts": []})


def steps(*descriptions: str) -> str:
    return json.dumps({"steps": [{"description": d} for d in descriptions]})


def verdict(passed: bool, *missing: str) -> str:
    return json.dumps({"passed": passed, "reason": "checked", "missing": list(missing)})


REMEMBERED = "notes.txt holds the answer 41."


def one_whole_run(answer: str) -> FakeLLM:
    return script(
        intent(),
        plan("Read notes.txt and say what it contains"),
        chooses("researcher"),
        steps("Read the file"),
        answer,
        verdict(True),
        REMEMBERED,
        verdict(True),
        answer,
    )


def wait_for_answer(client: TestClient, conversation_id: str) -> dict:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        thread = client.get(f"/api/conversations/{conversation_id}").json()
        messages = thread["messages"]
        if messages and messages[-1]["answered"]:
            return thread
        time.sleep(0.02)
    raise AssertionError(f"nothing was answered in {conversation_id}: {thread}")


# --- The shell's own questions -------------------------------------------------


def test_the_shell_can_tell_the_engine_is_up_before_asking_it_anything(
    tmp_path: Path,
) -> None:
    """What a desktop shell polls while it starts the runtime.

    It touches no storage, so a window polling every 200ms while the engine
    boots is not opening a database connection per poll - and the question it
    asks, "is the process answering", is the one a shell actually has.
    """
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(settings, FakeLLM()) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert "desktop" in health.json()["sources"]


@pytest.mark.parametrize(
    "origin",
    [
        # A packaged window serves its page from Tauri's own protocol. Missing
        # this one blocks every built application while every development run
        # keeps working, which is the shape of bug that ships.
        "tauri://localhost",
        "http://localhost:1420",
    ],
)
def test_a_desktop_window_may_talk_to_the_runtime_from_its_own_origin(
    tmp_path: Path, origin: str
) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(settings, FakeLLM()) as client:
        answered = client.get("/api/health", headers={"Origin": origin})
        assert answered.headers["access-control-allow-origin"] == origin


# --- A conversation ------------------------------------------------------------


def test_a_request_typed_into_a_window_is_carried_to_an_answer(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("the answer is 41", encoding="utf-8")

    with client_for(settings, one_whole_run("The notes say the answer is 41.")) as client:
        opened = client.post("/api/conversations", json={"title": ""})
        assert opened.status_code == 201
        conversation_id = opened.json()["id"]

        said = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"request": "What do my notes say?", "source": "desktop"},
        )
        assert said.status_code == 201
        assert said.json()["answered"] is False, "the window is told the work has begun"

        thread = wait_for_answer(client, conversation_id)
        assert "41" in thread["messages"][-1]["answer"]

        # The thread names itself after the first thing asked in it, so a window
        # listing threads has something to show without anybody titling one.
        assert thread["title"] == "What do my notes say?"

        # And the turn is the objective - one record, not two. The answer the
        # window shows is the one the manager's own history holds.
        objective = client.get(f"/api/objectives/{thread['messages'][-1]['id']}").json()
        assert objective["result"]["summary"] == thread["messages"][-1]["answer"]


def test_where_a_request_came_from_is_recorded_and_changes_nothing(tmp_path: Path) -> None:
    """The property the whole interface layer exists to keep.

    The same sentence from a desktop window and from a web page produces the
    same objective text and the same standard. If this ever fails, the core has
    acquired a favourite interface and every other one is the degraded path.
    """
    request = {"request": "What is SQLite?", "input_type": "text"}
    answer = "It keeps the whole database in one file."

    def asked_from(source: str) -> dict:
        settings = settings_for(tmp_path / source)
        create_schema(settings)
        with client_for(
            settings, script(intent(needs_work=False, answer=answer), "A", verdict(True))
        ) as client:
            objective_id = client.post(
                "/api/objectives", json={**request, "source": source}
            ).json()["id"]
            deadline = time.monotonic() + DEADLINE_SECONDS
            while time.monotonic() < deadline:
                found = client.get(f"/api/objectives/{objective_id}").json()
                if found["status"] in ("DONE", "FAILED", "ESCALATED"):
                    return found
                time.sleep(0.02)
            raise AssertionError("the objective never finished")

    (tmp_path / "desktop").mkdir()
    (tmp_path / "web").mkdir()

    from_desktop = asked_from("desktop")
    from_web = asked_from("web")

    assert from_desktop["text"] == from_web["text"]
    assert from_desktop["status"] == from_web["status"] == "DONE"
    assert from_desktop["acceptance_criteria"] == from_web["acceptance_criteria"]


def test_a_request_naming_a_thread_that_is_gone_is_still_done(tmp_path: Path) -> None:
    """The work is what the person asked for.

    A stale window id must not lose it. The request becomes a standalone
    objective, exactly like one stated from the CLI.
    """
    settings = settings_for(tmp_path)
    create_schema(settings)
    with client_for(
        settings, script(intent(needs_work=False, answer="Yes."), verdict(True))
    ) as client:
        created = client.post(
            "/api/objectives",
            json={
                "request": "Are you there?",
                "conversation_id": "11111111-1111-1111-1111-111111111111",
            },
        )
        assert created.status_code == 201
        assert created.json()["text"] == "Are you there?"


def test_the_activity_of_one_request_reads_as_one_piece_of_work(tmp_path: Path) -> None:
    """What the window draws while it waits.

    The manager's own progress and that of the employee it delegated to arrive
    on one stream, which is what makes the trace read as one run rather than as
    a manager talking to itself.
    """
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("the answer is 41", encoding="utf-8")

    with client_for(settings, one_whole_run("The notes say the answer is 41.")) as client:
        objective_id = client.post(
            "/api/objectives", json={"request": "What do my notes say?"}
        ).json()["id"]

        seen: list[dict] = []
        with client.stream("GET", f"/api/events?objective={objective_id}") as stream:
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))
                    if seen[-1]["kind"] == "RESULT" and seen[-1]["objective_id"]:
                        break

        kinds = {event["kind"] for event in seen}
        assert "STAGE" in kinds, "the manager says what it is doing"
        assert kinds & {"PLAN", "TOOL_CALL", "OBSERVATION"}, "and so does whoever did the work"
        assert any(event["objective_id"] == objective_id for event in seen)
        assert any(event["objective_id"] != objective_id for event in seen), (
            "an employee's own progress reaches the same stream"
        )


# --- Approvals -----------------------------------------------------------------


def test_an_irreversible_action_waits_for_the_window(tmp_path: Path) -> None:
    """The gate is the core's, and the window only carries the answer.

    Nothing here decides that writing a file is safe. The policy engine decided
    it needed asking; the run parks; a click answers it; the run continues.
    """
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "report.md").write_text("the old report", encoding="utf-8")
    llm = script(
        steps("Replace the report"),
        # Overwriting an existing file is the case the risk assessor raises to
        # HIGH, which is what makes this wait rather than proceed.
        tool_reply(
            ToolCallRequest(
                id="c1",
                name="fs.write",
                arguments={"path": "report.md", "content": "the new report"},
            )
        ),
        "Written.",
        verdict(True),
        REMEMBERED,
    )

    with client_for(settings, llm) as client:
        task_id = client.post(
            "/api/tasks", json={"goal": "Replace report.md", "employee": "organizer"}
        ).json()["id"]

        deadline = time.monotonic() + DEADLINE_SECONDS
        waiting: list[dict] = []
        while time.monotonic() < deadline and not waiting:
            waiting = client.get("/api/approvals").json()["approvals"]
            time.sleep(0.02)
        assert waiting, "the run parked on a question instead of acting"
        assert waiting[0]["live"] is True

        answered = client.post(f"/api/approvals/{waiting[0]['id']}", json={"approved": True})
        assert answered.status_code == 200
        assert answered.json()["state"] == "APPROVED"

        deadline = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["status"] in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.02)
        assert (workspace / "report.md").read_text(encoding="utf-8") == "the new report", (
            "the approved action happened"
        )
