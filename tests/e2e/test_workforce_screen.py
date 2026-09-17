"""Phase 11's required scenarios, through the application boundary a window uses.

The whole stack is real - FastAPI, the container, SQLite on a temporary file,
the manager, the employee runtime and the tools - with the model scripted. What
is asserted is what the Workforce screen receives: every verdict arrives
computed, and nothing has to be worked out from a list of tools.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from domain.integrations.models import Integration, IntegrationStatus
from domain.llm.models import ToolCallRequest
from domain.policies.models import ActorKind
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workforce.acceptance import Acceptance
from domain.workforce.assignment import TaskAssignment
from domain.workforce.protocols import Objective, ObjectiveStatus, Plan, PlanStatus
from infrastructure.employees.yaml_registry import employee_id_for
from infrastructure.persistence.models import Base
from infrastructure.persistence.session import create_engine
from tests.e2e.test_ask_prometheus import (
    REMEMBERED,
    intent,
    plan,
    script,
    steps,
    verdict,
    wait_for,
)
from tests.fakes.llm import FakeLLM, reply, scripted_models, tool_reply

REPO_ROOT = Path(__file__).resolve().parents[2]


def settings_for(tmp_path: Path, employees: Path | None = None) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}",
        file_root=tmp_path / "workspace",
        employees_dir=employees or REPO_ROOT / "employees",
        workflows_dir=tmp_path / "workflows",
        log_format="console",
    )


def create_schema(settings: Settings) -> None:
    async def _create() -> None:
        engine = create_engine(settings.resolved_database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())


def client_for(settings: Settings, llm: FakeLLM, holder: dict | None = None) -> TestClient:
    def build(resolved: Settings):
        container = build_container(resolved)
        scripted_models(container, llm)
        if holder is not None:
            holder["container"] = container
        return container

    return TestClient(create_app(settings, build=build))


# --- A ready employee does tool-backed work -----------------------------------


def test_a_ready_employee_does_tool_backed_work_and_its_record_shows_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "notes.txt").write_text("the answer is 41", encoding="utf-8")

    llm = script(
        intent(),
        plan("Read notes.txt and say what it contains"),
        json.dumps({"employee": "researcher", "reason": "it reads sources", "facts": []}),
        steps("Read the file"),
        tool_reply(ToolCallRequest(id="c1", name="fs.read", arguments={"path": "notes.txt"})),
        "The notes say the answer is 41.",
        verdict(True),
        REMEMBERED,
        verdict(True),
        "The notes say the answer is 41.",
    )

    with client_for(settings, llm) as client:
        roster = client.get("/api/workforce").json()
        assert roster["available"] is True
        researcher = next(item for item in roster["employees"] if item["name"] == "researcher")
        assert researcher["readiness"]["state"] in {"READY", "DEGRADED"}
        assert researcher["readiness"]["assignable"] is True

        objective_id = client.post(
            "/api/objectives", json={"request": "What do my notes say?"}
        ).json()["id"]
        assert wait_for(client, objective_id, "DONE", "FAILED", "ESCALATED")["status"] == "DONE"

        # The trace: why this employee, and who else was considered and why not.
        [task] = client.get(f"/api/work/{objective_id}").json()["tasks"]
        assert task["decision"]["code"] == "MODEL_CHOSE"
        assert task["decision"]["reason"] == "it reads sources"
        assert {item["code"] for item in task["decision"]["alternatives"]} == {"NOT_CHOSEN"}
        assert task["acceptance"]["accepted"] is True
        assert task["outcome"] == "ACCEPTED"

        # The profile: its record, from the stored assignment, with its sample said.
        profile = client.get("/api/workforce/researcher").json()
        record = profile["performance"]
        assert record["has_history"] is True
        assert record["assignments"] == 1
        assert record["outcomes"]["ACCEPTED"] == 1
        assert record["accepted_rate"]["value"] is None
        assert record["accepted_rate"]["note"] == "Not enough data: 1 of 5 decided assignments."
        assert record["cost_per_accepted_usd"]["sufficient"] is True
        assert record["derived_verdicts"] == 0
        [recent] = profile["recent_assignments"]
        assert recent["outcome"] == "ACCEPTED"
        assert recent["decision"]["code"] == "MODEL_CHOSE"
        assert profile["contract"]["declared"] is True
        assert profile["contract"]["evidence"] == ["TOOL_RESULT"]
        assert any(
            tool["name"] == "fs.read" and tool["effect"] == "READ" for tool in profile["tools"]
        )


def test_an_employee_with_no_history_has_no_data_rather_than_a_rate(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)

    with client_for(settings, FakeLLM([])) as client:
        record = client.get("/api/workforce/writer").json()["performance"]
        missing = client.get("/api/workforce/nobody")

    assert record["has_history"] is False
    assert record["assignments"] == 0
    for metric in ("accepted_rate", "cost_per_accepted_usd", "median_latency_seconds"):
        assert record[metric]["value"] is None
        assert record[metric]["sufficient"] is False
    assert record["accepted_rate"]["note"].startswith("No data")
    assert missing.status_code == 404


# --- An employee whose integration is missing ---------------------------------


def _declare(root: Path) -> Path:
    employees = root / "employees"
    for name, body in {
        "mailer": "integrations: [gmail]\ncapabilities: [TEXT_REASONING]\n",
        "reader": "allowed_tools: [fs.read]\ncapabilities: [FILE_ACCESS]\n",
    }.items():
        (employees / name).mkdir(parents=True)
        (employees / name / "employee.yaml").write_text(
            f"name: {name}\nrole: {name.title()}\n{body}", encoding="utf-8"
        )
    return employees


def test_a_missing_integration_is_unavailable_is_never_assigned_and_recovers_live(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path, _declare(tmp_path))
    create_schema(settings)
    (tmp_path / "workspace").mkdir(parents=True)
    (tmp_path / "workspace" / "notes.txt").write_text("41", encoding="utf-8")
    holder: dict = {}
    llm = script(
        intent(),
        plan("Read notes.txt"),
        # No choosing call: with the mailer unavailable there is one candidate.
        steps("Read it"),
        tool_reply(ToolCallRequest(id="c1", name="fs.read", arguments={"path": "notes.txt"})),
        "41.",
        verdict(True),
        REMEMBERED,
        verdict(True),
        "41.",
    )

    with client_for(settings, llm, holder) as client:
        mailer = next(
            item
            for item in client.get("/api/workforce").json()["employees"]
            if item["name"] == "mailer"
        )
        assert mailer["readiness"]["state"] == "UNAVAILABLE"
        [reason] = [
            item for item in mailer["readiness"]["reasons"] if item["state"] == "UNAVAILABLE"
        ]
        assert reason["code"] == "INTEGRATION_NOT_CONNECTED"
        assert reason["recovery"] == "PLUGINS"

        objective_id = client.post("/api/objectives", json={"request": "Read my notes"}).json()[
            "id"
        ]
        assert wait_for(client, objective_id, "DONE", "FAILED", "ESCALATED")["status"] == "DONE"
        [task] = client.get(f"/api/work/{objective_id}").json()["tasks"]
        assert task["employee"] == "reader"
        assert task["decision"]["code"] == "ONLY_QUALIFIED"
        [passed_over] = task["decision"]["alternatives"]
        assert (passed_over["employee"], passed_over["code"]) == ("mailer", "UNAVAILABLE")

        # Connecting the service is seen on the next read, with no restart.
        holder["container"].refresh_grants(
            [Integration(id=uuid4(), name="gmail", status=IntegrationStatus.CONNECTED)]
        )
        mailer = client.get("/api/workforce/mailer").json()
        assert mailer["readiness"]["state"] == "READY"
        assert mailer["integrations"] == [{"name": "gmail", "declared": True, "connected": True}]


# --- Workflow suggestions -----------------------------------------------------


def _seed_runs(settings: Settings, count: int) -> None:
    from infrastructure.employees.yaml_registry import YamlEmployeeRegistry
    from infrastructure.persistence.assignment_repository import SqlAssignmentRepository
    from infrastructure.persistence.employee_repository import SqlEmployeeRepository
    from infrastructure.persistence.objective_repository import SqlObjectiveRepository
    from infrastructure.persistence.plan_repository import SqlPlanRepository
    from infrastructure.persistence.session import create_session_factory
    from infrastructure.persistence.task_repository import SqlTaskRepository

    async def seed() -> None:
        engine = create_engine(settings.resolved_database_url)
        factory = create_session_factory(engine)
        await SqlEmployeeRepository(factory).sync(
            YamlEmployeeRegistry(settings.employees_dir).list()
        )
        for _ in range(count):
            objective = replace(
                Objective.create("Private request text that must not be copied"),
                status=ObjectiveStatus.DONE,
                finished_at=datetime.now(UTC),
            )
            await SqlObjectiveRepository(factory).save(objective)
            gather, write = Task.create("Gather"), Task.create("Write")
            recorded = Plan.create(
                objective.id,
                tasks=(gather, write),
                dependencies=((write.id, gather.id),),
                status=PlanStatus.DONE,
            )
            await SqlPlanRepository(factory).save(recorded)
            for task, name in ((gather, "researcher"), (write, "writer")):
                await SqlTaskRepository(factory).save(
                    replace(
                        task,
                        plan_id=recorded.id,
                        assigned_employee_id=employee_id_for(name),
                        status=TaskStatus.COMPLETED,
                        result=TaskResult(summary="done"),
                    )
                )
                await SqlAssignmentRepository(factory).save(
                    TaskAssignment.create(
                        task_id=task.id,
                        employee_id=employee_id_for(name),
                        assigned_by=ActorKind.PROMETHEUS,
                    ).judged(Acceptance.taken())
                )
        await engine.dispose()

    asyncio.run(seed())


def test_similar_successful_runs_give_one_draft_that_dismiss_silences(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    _seed_runs(settings, 3)

    with client_for(settings, FakeLLM([])) as client:
        first = client.get("/api/workflow-suggestions").json()
        assert first["available"] is True
        [draft] = first["suggestions"]
        assert draft["occurrences"] == 3
        assert [step["employee"] for step in draft["steps"]] == ["researcher", "writer"]
        assert draft["steps"][1]["depends_on"] == ["step-1"]
        assert "WRITE" in draft["steps"][1]["effects"]
        assert "Private request text" not in json.dumps(first)
        assert len(client.get("/api/workflow-suggestions").json()["suggestions"]) == 1

        dismissed = client.post(f"/api/workflow-suggestions/{draft['id']}/dismiss")
        assert dismissed.status_code == 200
        assert client.get("/api/workflow-suggestions").json()["suggestions"] == []


def test_saving_a_draft_is_its_own_confirmation_and_writes_a_manual_workflow(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)
    (tmp_path / "workflows").mkdir()
    _seed_runs(settings, 3)

    with client_for(settings, FakeLLM([])) as client:
        [draft] = client.get("/api/workflow-suggestions").json()["suggestions"]
        invalid = client.post(
            f"/api/workflow-suggestions/{draft['id']}/save", json={"name": "Not A Name"}
        )
        saved = client.post(
            f"/api/workflow-suggestions/{draft['id']}/save", json={"name": "notes-digest"}
        )
        workflows = client.get("/api/workflows").json()["workflows"]
        again = client.get("/api/workflow-suggestions").json()["suggestions"]
        schedules = client.get("/api/schedules").json()["schedules"]

    assert invalid.status_code == 422
    assert saved.status_code == 201
    assert saved.json()["file"] == "notes-digest.yaml"
    assert any(item["name"] == "notes-digest" for item in workflows)
    assert again == []
    assert schedules == [], "saving scheduled nothing"


def test_an_unknown_suggestion_is_not_found(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_schema(settings)

    with client_for(settings, FakeLLM([reply("")])) as client:
        response = client.post(f"/api/workflow-suggestions/{uuid4()}/dismiss")

    assert response.status_code == 404
