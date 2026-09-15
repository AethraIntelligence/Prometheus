"""The files a turn shows are the files the store says were written."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from application.interface.artifacts import produced_files, within
from domain.tasks.task import Task
from domain.tools.telemetry import ToolCallRecord
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus, Plan
from tests.unit.test_interface_boundary import build

START = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def call(tool: str, output: dict, *, success: bool = True, at: int = 0, task_id=None):
    return ToolCallRecord(
        tool=tool,
        success=success,
        output=output,
        task_id=task_id,
        created_at=START + timedelta(seconds=at),
    )


def test_a_file_is_listed_once_where_it_now_is() -> None:
    calls = [
        call("fs.write", {"path": "draft.md", "bytes_written": 10}, at=0),
        call("fs.write", {"path": "notes.txt", "bytes_written": 3}, at=1),
        call("fs.write", {"path": "draft.md", "bytes_written": 12, "overwritten": True}, at=2),
        call("fs.move", {"source": "draft.md", "destination": "final/report.md"}, at=3),
    ]

    assert produced_files(calls) == ["final/report.md", "notes.txt"]


def test_what_failed_or_only_read_is_not_a_file_somebody_can_open() -> None:
    calls = [
        call("fs.write", {"path": "x.md", "bytes_written": 1}, success=False),
        call("fs.read", {"path": "input.csv", "content": "a,b"}),
        call("fs.move", {"source": "input.csv", "destination": "old/input.csv"}),
    ]

    assert produced_files(calls) == []


def test_a_path_that_leaves_the_root_leads_nowhere(tmp_path: Path) -> None:
    assert within(tmp_path, "../secret") is None
    assert within(tmp_path, "a/b.md") == (tmp_path / "a" / "b.md").resolve()


class Plans:
    def __init__(self, *plans: Plan) -> None:
        self._plans = plans

    async def save(self, plan):
        return None

    async def get(self, plan_id):
        return None

    async def for_objective(self, objective_id):
        return [plan for plan in self._plans if plan.objective_id == objective_id]


class Calls:
    def __init__(self, *calls: ToolCallRecord) -> None:
        self._calls = calls

    async def list_for_task(self, task_id):
        return [item for item in self._calls if item.task_id == task_id]


class Workspaces:
    def __init__(self, root: Path) -> None:
        self._root = root

    def root_for(self, workspace_id):
        return self._root


async def test_a_turn_carries_the_files_its_work_wrote_and_serves_only_those(
    tmp_path: Path,
) -> None:
    service, parts = build()
    thread = await service.create_conversation()
    task = Task.create("Write the news")
    objective = Objective.create("Check the news", conversation_id=UUID(thread["id"]))
    objective = objective.to(
        ObjectiveStatus.DONE, ObjectiveResult(objective.id, "Saved.", ObjectiveStatus.DONE)
    )
    await parts["objectives"].save(objective)
    (tmp_path / "raw_news.txt").write_text("1. News")
    (tmp_path / "private.txt").write_text("not the work's")
    service._d = replace(
        service._d,
        plans=Plans(Plan.create(objective.id, tasks=(task,))),
        tool_calls=Calls(
            call("fs.write", {"path": "raw_news.txt", "bytes_written": 7}, task_id=task.id)
        ),
        workspaces=Workspaces(tmp_path),
    )

    opened = await service.get_conversation(UUID(thread["id"]))
    [produced] = opened["messages"][0]["artifacts"]

    assert produced["name"] == "raw_news.txt"
    assert produced["exists"] is True
    assert produced["media_type"] == "text/plain"
    found = await service.artifact_file(objective.id, "raw_news.txt")
    assert found is not None and found[0].read_text() == "1. News"
    assert await service.artifact_file(objective.id, "private.txt") is None
    assert await service.artifact_file(uuid4(), "raw_news.txt") is None
