"""A hard process exit keeps manager intent and the external-effect ledger."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from uuid import UUID

from domain.tools.telemetry import ToolCallRecord
from infrastructure.persistence.objective_repository import SqlObjectiveRepository
from infrastructure.persistence.plan_repository import SqlPlanRepository
from infrastructure.persistence.session import create_engine, create_session_factory
from infrastructure.persistence.tool_call_repository import SqlToolCallLog

CHILD = textwrap.dedent(
    """
    import asyncio
    import json
    import os
    import sys
    from dataclasses import replace

    from domain.tasks.task import Task, TaskCreatedBy
    from domain.tools.telemetry import ToolCallRecord
    from domain.workforce.protocols import Objective, ObjectiveStatus, Plan, PlanStatus
    from infrastructure.persistence.models import Base
    from infrastructure.persistence.objective_repository import SqlObjectiveRepository
    from infrastructure.persistence.plan_repository import SqlPlanRepository
    from infrastructure.persistence.session import create_engine, create_session_factory
    from infrastructure.persistence.tool_call_repository import SqlToolCallLog


    async def main():
        engine = create_engine(f"sqlite+aiosqlite:///{sys.argv[1]}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        objectives = SqlObjectiveRepository(factory)
        plans = SqlPlanRepository(factory)
        calls = SqlToolCallLog(factory)

        objective = replace(
            Objective.create("Publish the prepared report"),
            status=ObjectiveStatus.RUNNING,
            acceptance_criteria=("the report is published",),
        )
        task = Task.create("Publish the report", created_by=TaskCreatedBy.PROMETHEUS)
        plan = Plan.create(
            objective.id,
            tasks=(task,),
            status=PlanStatus.RUNNING,
        )
        await objectives.save(objective)
        await plans.save(plan)

        intent = ToolCallRecord(
            tool="api.publish",
            success=False,
            task_id=task.id,
            call_id="publish-once",
            completed=False,
            input_data={"document": "report.md"},
        )
        assert await calls.reserve(intent)
        await calls.complete(
            replace(
                intent,
                success=True,
                completed=True,
                output={"publication_id": "pub-1"},
            )
        )

        identifiers = {
            "objective": str(objective.id),
            "plan": str(plan.id),
            "task": str(task.id),
        }
        print(json.dumps(identifiers), flush=True)
        os._exit(23)


    asyncio.run(main())
    """
)


async def test_hard_exit_keeps_the_plan_and_prevents_a_repeated_effect(tmp_path: Path) -> None:
    database = tmp_path / "hard-crash.db"
    process = subprocess.run(
        [sys.executable, "-c", CHILD, str(database)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert process.returncode == 23, process.stderr
    identifiers = json.loads(process.stdout.strip().splitlines()[-1])

    engine = create_engine(f"sqlite+aiosqlite:///{database}")
    factory = create_session_factory(engine)
    objectives = SqlObjectiveRepository(factory)
    plans = SqlPlanRepository(factory)
    calls = SqlToolCallLog(factory)

    incomplete = await objectives.list_incomplete()
    assert [str(item.id) for item in incomplete] == [identifiers["objective"]]
    recovered = await plans.get(UUID(identifiers["plan"]))
    assert recovered is not None
    assert [str(task.id) for task in recovered.tasks] == [identifiers["task"]]

    duplicate = ToolCallRecord(
        tool="api.publish",
        success=False,
        task_id=UUID(identifiers["task"]),
        call_id="publish-once",
        completed=False,
        input_data={"document": "report.md"},
    )
    assert not await calls.reserve(duplicate), "the restarted process cannot claim it twice"
    previous = await calls.get_call(UUID(identifiers["task"]), "publish-once")
    assert previous is not None and previous.completed
    assert previous.output == {"publication_id": "pub-1"}

    await engine.dispose()
