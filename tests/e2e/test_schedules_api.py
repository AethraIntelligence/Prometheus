"""Scheduled work through the surface a person uses.

A schedule made from the window gets a thread, reads back with what the
scheduler will do with it, pauses, resumes, runs now into its own thread and is
deleted without taking the thread with it.

The workflow half declares its own process in a temporary directory, the way
`test_governance.py` does. Nothing ships in `workflows/` any more: a person
setting work on a clock never chooses a process, so a catalog entry on a fresh
installation was a question with no answer behind it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from time import sleep

import pytest
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from tests.e2e.test_memory_settings import create_schema, settings_for
from tests.fakes.llm import FakeLLM, reply

WORKFLOW = """
name: weekly-report
version: 1
description: A declared process, for the half of this file that needs one.
trigger: MANUAL
inputs:
  folder:
    type: STRING
    default: "."
    description: Folder containing the source material.
  report:
    type: STRING
    default: weekly-report.md
    description: Output file written by the final step.
budget:
  max_steps: 4
steps:
  - name: survey
    employee: organizer
    instruction: List everything under {folder} and say what is there.
  - name: write_up
    employee: researcher
    depends_on: [survey]
    instruction: Write {report} from {steps.survey}.
"""


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    declared = tmp_path / "workflows"
    declared.mkdir()
    (declared / "weekly-report.yaml").write_text(WORKFLOW, encoding="utf-8")
    settings = settings_for(tmp_path).model_copy(update={"workflows_dir": declared})
    create_schema(settings)

    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: FakeLLM([reply("")] * 50)  # type: ignore[method-assign]
        return container

    with TestClient(create_app(settings, build=build)) as client:
        yield client


def test_a_schedule_is_made_listed_paused_and_removed(client: TestClient) -> None:
    listing = client.get("/api/schedules").json()
    assert listing == {"available": True, "running": False, "schedules": []}

    made = client.post(
        "/api/schedules",
        json={
            "request": "Summarise yesterday's notes",
            "daily_at": "09:30",
            "utc_offset_minutes": 180,
        },
    )
    assert made.status_code == 201, made.text
    schedule = made.json()
    assert schedule["daily_at_utc"] == "06:30", "the person's clock is stored as UTC"
    assert schedule["enabled"] is True
    assert schedule["conversation_id"]
    assert schedule["overlap_policy"] == "SKIP"
    assert schedule["misfire_policy"] == "COALESCE"

    thread = client.get(f"/api/conversations/{schedule['conversation_id']}")
    assert thread.status_code == 200
    assert thread.json()["title"] == "Summarise yesterday's notes"

    paused = client.patch(f"/api/schedules/{schedule['id']}", json={"enabled": False}).json()
    assert paused["enabled"] is False

    assert client.delete(f"/api/schedules/{schedule['id']}").json() == {"removed": True}
    assert client.get("/api/schedules").json()["schedules"] == []
    assert client.get(f"/api/conversations/{schedule['conversation_id']}").status_code == 200


def test_a_workflow_is_checked_and_pinned_before_it_is_scheduled(
    client: TestClient,
) -> None:
    catalog = client.get("/api/workflows")
    assert catalog.status_code == 200
    workflow = next(
        item for item in catalog.json()["workflows"] if item["name"] == "weekly-report"
    )
    assert workflow["version"] == 1
    assert workflow["readiness"] == {"ready": True, "issues": []}
    assert workflow["inputs"][0]["kind"] == "STRING"
    assert workflow["budget"]["max_steps"] == 4

    preview = client.post(
        "/api/workflows/weekly-report/dry-run",
        json={"version": 1, "inputs": {"folder": "sales", "report": "week.md"}},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["executable"] is True
    assert preview.json()["steps"][0]["instruction"].startswith("List everything under sales")

    made = client.post(
        "/api/schedules",
        json={
            "workflow_name": "weekly-report",
            "workflow_version": 1,
            "workflow_inputs": {"folder": "sales", "report": "week.md"},
            "daily_at": "09:00",
            "name": "Weekly sales report",
        },
    )
    assert made.status_code == 201, made.text
    schedule = made.json()
    assert schedule["workflow_name"] == "weekly-report"
    assert schedule["workflow_version"] == 1
    assert schedule["workflow_inputs"] == {"folder": "sales", "report": "week.md"}
    assert schedule["conversation_id"] is None
    assert schedule["retry_policy"] == "DECLARED"
    assert schedule["misfire_policy"] == "COALESCE"
    assert schedule["overlap_policy"] == "SKIP"


def test_a_schedule_with_nothing_repeated_is_offered_no_order(client: TestClient) -> None:
    """The quiet case, and the one every schedule starts in.

    A settled order is offered off the record of a schedule's own successful
    runs. A schedule that has not run has no record, so the answer is an empty
    list rather than a suggestion somebody has to decide about.
    """
    made = client.post(
        "/api/schedules", json={"request": "Check the news", "daily_at": "09:00"}
    ).json()

    offered = client.get(f"/api/schedules/{made['id']}/suggestions")

    assert offered.status_code == 200, offered.text
    assert offered.json() == {"available": True, "suggestions": []}


def test_an_order_is_unpinned_and_the_schedule_asks_the_long_way_again(
    client: TestClient,
) -> None:
    """Unpinning is a way back, and it destroys nothing.

    The declaration stays on disk - which is what makes confirming one a
    reversible decision rather than a door in one direction.
    """
    made = client.post(
        "/api/schedules",
        json={
            "workflow_name": "weekly-report",
            "workflow_version": 1,
            "daily_at": "09:00",
            "name": "Weekly sales report",
        },
    ).json()
    assert made["workflow_name"] == "weekly-report"

    loosened = client.delete(f"/api/schedules/{made['id']}/order")

    assert loosened.status_code == 200, loosened.text
    assert loosened.json()["workflow_name"] == ""
    assert loosened.json()["workflow_version"] is None
    assert client.get("/api/workflows").json()["workflows"], "the declaration is still declared"


def test_an_order_nobody_was_offered_is_refused(client: TestClient) -> None:
    made = client.post(
        "/api/schedules", json={"request": "Check the news", "daily_at": "09:00"}
    ).json()

    refused = client.post(
        f"/api/schedules/{made['id']}/order",
        json={"suggestion_id": "00000000-0000-0000-0000-000000000001"},
    )

    assert refused.status_code == 404, refused.text


@pytest.mark.parametrize(
    "body",
    [
        {"request": "x"},
        {"request": "x", "every_minutes": 60, "on_event": "inbox.arrived"},
        {"request": "x", "every_minutes": 0},
        {"request": "x", "daily_at": "25:99"},
        {"request": "   ", "every_minutes": 60},
    ],
)
def test_a_schedule_that_cannot_mean_one_thing_is_refused(client: TestClient, body: dict) -> None:
    refused = client.post("/api/schedules", json=body)
    assert refused.status_code == 400, refused.text


def test_run_now_asks_in_the_schedules_own_thread(client: TestClient) -> None:
    made = client.post(
        "/api/schedules", json={"request": "Say hello", "every_minutes": 120, "name": "Hello"}
    ).json()

    ran = client.post(f"/api/schedules/{made['id']}/run")

    assert ran.status_code == 201, ran.text
    assert ran.json()["conversation_id"] == made["conversation_id"]
    listed = client.get("/api/schedules").json()["schedules"][0]
    for _ in range(50):
        if listed["runs"] == 1:
            break
        sleep(0.02)
        listed = client.get("/api/schedules").json()["schedules"][0]
    assert listed["runs"] == 1
    assert listed["recent_runs"][0]["objective_id"] == ran.json()["id"]
    assert listed["recent_runs"][0]["schedule_version"] == 1


def test_a_schedule_made_from_a_thread_repeats_into_that_thread(client: TestClient) -> None:
    thread = client.post("/api/conversations", json={"title": "Weekly report"}).json()

    made = client.post(
        "/api/schedules",
        json={
            "request": "Write the weekly report",
            "every_minutes": 10080,
            "conversation_id": thread["id"],
        },
    ).json()

    assert made["conversation_id"] == thread["id"]


def test_a_schedule_is_edited_in_place_and_its_model_is_checked(client: TestClient) -> None:
    made = client.post(
        "/api/schedules", json={"request": "Check the news", "daily_at": "09:00"}
    ).json()
    models = [m for m in client.get("/api/providers").json()["models"] if m["generates_text"]]
    assert models, "the shipped catalog has models to choose from"
    chosen = models[0]["name"]

    unknown = client.put(
        f"/api/schedules/{made['id']}",
        json={"request": "Check the news", "daily_at": "09:00", "model": "nobody-has-this"},
    )
    assert unknown.status_code == 400
    assert "no model called" in unknown.json()["detail"]

    edited = client.put(
        f"/api/schedules/{made['id']}",
        json={
            "request": "Check 10 news in IT and AI",
            "name": "Morning research",
            "every_minutes": 180,
            "model": chosen,
        },
    )

    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["id"], body["conversation_id"]) == (made["id"], made["conversation_id"])
    assert (body["request"], body["name"], body["model"]) == (
        "Check 10 news in IT and AI",
        "Morning research",
        chosen,
    )
    assert (body["every_seconds"], body["daily_at_utc"]) == (10800, None)
    assert client.get("/api/schedules").json()["schedules"][0]["model"] == chosen


def test_approvals_are_chosen_edited_checked_and_shown_in_the_thread(client: TestClient) -> None:
    made = client.post(
        "/api/schedules",
        json={"request": "Tidy the downloads", "every_minutes": 60, "approvals": "DENY"},
    ).json()
    assert made["approvals"] == "DENY"

    thread = client.get(f"/api/conversations/{made['conversation_id']}").json()
    assert thread["schedule_id"] == made["id"]
    assert _how(thread["directions"]) == {"approvals": "DENY", "model": ""}

    refused = client.put(
        f"/api/schedules/{made['id']}",
        json={"request": "Tidy the downloads", "every_minutes": 60, "approvals": "MAYBE"},
    )
    assert refused.status_code == 400

    edited = client.put(
        f"/api/schedules/{made['id']}",
        json={"request": "Tidy the downloads", "every_minutes": 60, "approvals": "AUTO"},
    ).json()
    assert edited["approvals"] == "AUTO"
    thread = client.get(f"/api/conversations/{made['conversation_id']}").json()
    assert thread["directions"]["approvals"] == "AUTO"


def test_a_thread_reopens_with_how_its_last_request_was_asked(client: TestClient) -> None:
    thread = client.post("/api/conversations", json={"title": "Plain"}).json()
    assert client.get(f"/api/conversations/{thread['id']}").json()["directions"] is None

    sent = client.post(
        f"/api/conversations/{thread['id']}/messages",
        json={"request": "Say hello", "approvals": "DENY", "source": "desktop"},
    )
    assert sent.status_code == 201, sent.text
    assert _how(sent.json()["directions"]) == {"approvals": "DENY", "model": ""}

    reopened = client.get(f"/api/conversations/{thread['id']}").json()
    assert reopened["schedule_id"] is None
    assert _how(reopened["directions"]) == {"approvals": "DENY", "model": ""}
    assert reopened["messages"][0]["directions"]["approvals"] == "DENY"


def test_a_thread_keeps_an_approval_change_made_between_requests(client: TestClient) -> None:
    thread = client.post("/api/conversations", json={"title": "Plain"}).json()
    client.post(
        f"/api/conversations/{thread['id']}/messages",
        json={"request": "Say hello", "approvals": "ASK", "source": "desktop"},
    )

    changed = client.put(
        f"/api/conversations/{thread['id']}/approvals", json={"approvals": "AUTO"}
    )

    assert changed.status_code == 200
    assert changed.json()["directions"]["approvals"] == "AUTO"
    reopened = client.get(f"/api/conversations/{thread['id']}").json()
    assert reopened["directions"]["approvals"] == "AUTO"
    assert reopened["messages"][0]["directions"]["approvals"] == "ASK"

    refused = client.put(
        f"/api/conversations/{thread['id']}/approvals", json={"approvals": "MAYBE"}
    )
    assert refused.status_code == 422


def test_a_thread_keeps_a_model_change_made_between_requests(client: TestClient) -> None:
    thread = client.post("/api/conversations", json={"title": "Plain"}).json()
    client.post(
        f"/api/conversations/{thread['id']}/messages",
        json={"request": "Say hello", "model": "first-model", "source": "desktop"},
    )

    changed = client.put(
        f"/api/conversations/{thread['id']}/model", json={"model": "next-model"}
    )

    assert changed.status_code == 200
    assert changed.json()["directions"]["model"] == "next-model"
    reopened = client.get(f"/api/conversations/{thread['id']}").json()
    assert reopened["directions"]["model"] == "next-model"
    assert reopened["messages"][0]["directions"]["model"] == "first-model"

    reset = client.put(f"/api/conversations/{thread['id']}/model", json={"model": ""})
    assert reset.json()["directions"]["model"] == ""


def _how(directions: dict) -> dict:
    """Approvals and model only: the folder is the thread's, and checked where folders are."""
    return {key: directions[key] for key in ("approvals", "model")}
