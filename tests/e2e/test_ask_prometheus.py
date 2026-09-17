"""Phase 7's Definition of Done, through the surfaces a person actually uses.

*The user states a goal in one sentence and gets a verified result, without ever
addressing an employee.* So nothing here names one: the requests are sentences,
and which employee did what is read back afterwards as evidence, not supplied.

The whole stack is real - the CLI, FastAPI, the container, SQLite on a temporary
file, the manager, the employee runtime - with one thing scripted: the model,
because the suite runs without a provider key.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from domain.llm.models import ToolCallRequest
from infrastructure.persistence.models import Base
from infrastructure.persistence.session import create_engine
from tests.fakes.llm import FakeLLM, reply, scripted_models, tool_reply

REPO_ROOT = Path(__file__).resolve().parents[2]
DEADLINE_SECONDS = 10.0


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}",
        file_root=tmp_path / "workspace",
        employees_dir=REPO_ROOT / "employees",
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
    """One client for every stage, answering in order.

    A string is a plain reply; anything else is passed through, which is how a
    tool call gets into the middle of an employee's run.
    """
    return FakeLLM([reply(item) if isinstance(item, str) else item for item in answers])


def client_for(settings: Settings, llm: FakeLLM) -> TestClient:
    def build(resolved: Settings):
        container = build_container(resolved)
        scripted_models(container, llm)
        return container

    return TestClient(create_app(settings, build=build))


def wait_for(client: TestClient, objective_id: str, *statuses: str) -> dict:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        objective = client.get(f"/api/objectives/{objective_id}").json()
        if objective["status"] in statuses:
            return objective
        time.sleep(0.02)
    raise AssertionError(f"objective stayed {objective['status']}, wanted {statuses}")


# --- What the model is told to say --------------------------------------------


def intent(**overrides) -> str:
    return json.dumps(
        {
            "restatement": "read the notes and write a summary",
            "constraints": {},
            "acceptance_criteria": ["summary.md exists"],
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


#: What the distiller is told to write when a task reaches a terminal state.
#: It is part of the story rather than an interception, because it is a real
#: model call in a real run: what an employee said is not what gets remembered.
REMEMBERED = "notes.txt holds the answer 41."


# --- The interface ------------------------------------------------------------


def test_a_goal_stated_in_one_sentence_is_carried_to_a_result(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("the answer is 41", encoding="utf-8")

    llm = script(
        intent(),                       # Prometheus reads the request
        plan("Read notes.txt and say what it contains"),
        chooses("researcher"),          # Prometheus picks, from the registry's own names
        steps("Read the file"),         # the employee plans its own steps
        "The notes say the answer is 41.",
        verdict(True),                  # the employee's own verifier
        REMEMBERED,                     # what the run leaves in memory
        verdict(True),                  # Prometheus's check against the objective
        "The notes say the answer is 41.",  # the answer the user reads
    )

    with client_for(settings, llm) as client:
        created = client.post("/api/objectives", json={"request": "What do my notes say?"})
        assert created.status_code == 201
        objective_id = created.json()["id"]
        assert created.json()["thinking"] is True

        finished = wait_for(client, objective_id, "DONE", "FAILED", "ESCALATED")
        assert finished["status"] == "DONE", finished
        assert "41" in finished["result"]["summary"]
        assert finished["acceptance_criteria"] == ["summary.md exists"]

        # The evidence: one plan, one task, and who did it.
        assert len(finished["plans"]) == 1
        planned = finished["plans"][0]
        assert planned["revision"] == 1
        assert [task["status"] for task in planned["tasks"]] == ["COMPLETED"]
        assert finished["result"]["output"]["tasks"][0]["employee"] == "researcher"

        # And it is in the history without anyone naming an employee to get it.
        history = client.get("/api/objectives").json()["objectives"]
        assert [item["id"] for item in history] == [objective_id]
        assert history[0]["text"] == "What do my notes say?"


def test_a_question_needing_no_work_is_answered_without_a_plan(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    llm = script(
        intent(needs_work=False, answer="It keeps the whole database in one file."),
        # The second opinion on "no work": a reply, not a lookup.
        "A",
        verdict(True),
    )

    with client_for(settings, llm) as client:
        objective_id = client.post(
            "/api/objectives", json={"request": "What is SQLite?"}
        ).json()["id"]

        finished = wait_for(client, objective_id, "DONE", "FAILED", "ESCALATED")
        assert finished["status"] == "DONE"
        assert "one file" in finished["result"]["summary"]
        assert finished["plans"] == [], "no decomposition, and nobody was given work"
        assert client.get("/api/tasks").json()["tasks"] == [], "no task was ever created"


def test_the_trace_shows_the_manager_and_its_employee_as_one_story(tmp_path: Path) -> None:
    """6.3 at the objective level: Prometheus's own progress, and its tasks', merged."""
    settings = settings_for(tmp_path)
    create_schema(settings)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)

    (tmp_path / "workspace" / "notes.txt").write_text("41", encoding="utf-8")
    llm = script(
        intent(),
        plan("Read the notes"),
        chooses("researcher"),
        steps("Read the file"),
        # The employee reaches for a tool, which is what puts an observation
        # from inside the task into the objective's trace.
        tool_reply(ToolCallRequest(id="c1", name="fs.read", arguments={"path": "notes.txt"})),
        "The notes say 41.",
        verdict(True),
        REMEMBERED,
        verdict(True),
        "The notes say 41.",
    )

    with client_for(settings, llm) as client:
        objective_id = client.post(
            "/api/objectives", json={"request": "What do the notes say?"}
        ).json()["id"]
        wait_for(client, objective_id, "DONE", "FAILED", "ESCALATED")

        stream = client.get(f"/api/events?objective={objective_id}")
        assert stream.status_code == 200
        events = [
            json.loads(line[6:])
            for line in stream.text.splitlines()
            if line.startswith("data: ")
        ]

        kinds = [event["kind"] for event in events]
        assert kinds[0] == "STAGE", "the manager says it is reading the request first"
        assert "PLAN" in kinds
        assert kinds[-1] == "RESULT"

        # Both voices are in it: the manager's, stamped with the objective, and
        # the employee's, stamped only with its task.
        assert any(event["objective_id"] == objective_id for event in events)
        assert any(event["objective_id"] is None for event in events), (
            "the employee's own progress is in the same trace"
        )
        assert any(
            event["kind"] == "OBSERVATION" and "fs.read returned" in event["message"]
            for event in events
        ), "including what its tools came back with"


def test_an_unmet_objective_is_escalated_with_what_is_missing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)

    # Both attempts produce work; neither satisfies the objective.
    attempt = [
        chooses("researcher"),
        steps("Look"),
        "I found eleven.",
        verdict(True),
        REMEMBERED,
    ]
    llm = script(
        intent(),
        plan("Find twenty things"),
        *attempt,
        verdict(False, "only eleven were found"),
        plan("Find twenty things, properly"),
        *attempt,
        verdict(False, "only eleven were found"),
        "I found eleven of the twenty.",
    )

    with client_for(settings, llm) as client:
        objective_id = client.post(
            "/api/objectives", json={"request": "Find twenty things"}
        ).json()["id"]

        finished = wait_for(client, objective_id, "DONE", "FAILED", "ESCALATED")
        assert finished["status"] == "ESCALATED"
        assert finished["result"]["missing"] == ["only eleven were found"]
        assert "eleven" in finished["result"]["summary"], "the work done is still handed over"

        # Both revisions are visible, and the first is marked as superseded.
        revisions = {plan_["revision"]: plan_["status"] for plan_ in finished["plans"]}
        assert revisions == {2: "FAILED", 1: "SUPERSEDED"}


# --- The command line ---------------------------------------------------------


def test_ask_prometheus_reports_the_answer_and_who_produced_it(tmp_path, monkeypatch) -> None:
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "PROMETHEUS_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}"
    )
    monkeypatch.setenv("PROMETHEUS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("PROMETHEUS_EMPLOYEES_DIR", str(REPO_ROOT / "employees"))
    try:
        create_schema(get_settings())
        llm = script(
            intent(),
            plan("Say something"),
            chooses("researcher"),
            steps("Say it"),
            "Said.",
            verdict(True),
            REMEMBERED,
            verdict(True),
            "Said, and here it is.",
        )

        import app.cli.main as cli

        original = cli.build_container
        monkeypatch.setattr(
            cli,
            "build_container",
            lambda *args, **kwargs: _scripted(original(*args, **kwargs), llm),
        )

        result = CliRunner().invoke(cli.app, ["ask-prometheus", "Say something for me"])
        assert result.exit_code == 0, result.output
        assert "Said, and here it is." in result.output
        assert "researcher" in result.output, "who did it is on screen, not looked up"
        assert "[DONE]" in result.output

        listed = CliRunner().invoke(cli.app, ["objectives"])
        assert listed.exit_code == 0
        assert "Say something for me" in listed.output
    finally:
        get_settings.cache_clear()


def _scripted(container, llm: FakeLLM):
    scripted_models(container, llm)
    return container
