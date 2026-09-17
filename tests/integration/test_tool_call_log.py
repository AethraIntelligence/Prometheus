"""Tool-call accounting against the same contract, in memory and on SQLite."""

from __future__ import annotations

from uuid import uuid4

import pytest

from domain.tools.telemetry import ToolCallRecord
from infrastructure.persistence.tool_call_repository import (
    InMemoryToolCallLog,
    SqlToolCallLog,
)


@pytest.fixture(params=["in_memory", "sqlite"])
def call_log(request: pytest.FixtureRequest):
    if request.param == "in_memory":
        return InMemoryToolCallLog()
    return SqlToolCallLog(request.getfixturevalue("session_factory"))


def call(tool: str = "fs.read", *, task_id=None, success: bool = True) -> ToolCallRecord:
    return ToolCallRecord(
        tool=tool,
        success=success,
        latency_ms=12,
        task_id=task_id,
        input_data={"path": "notes.txt"},
        output={"content": "hello"},
        error=None if success else "boom",
    )


async def test_a_task_with_no_calls_reads_back_empty(call_log) -> None:
    assert await call_log.list_for_task(uuid4()) == []


async def test_calls_come_back_in_the_order_they_happened(call_log) -> None:
    task_id = uuid4()
    for tool in ("fs.list", "fs.read", "fs.move"):
        await call_log.record(call(tool, task_id=task_id))

    assert [c.tool for c in await call_log.list_for_task(task_id)] == [
        "fs.list",
        "fs.read",
        "fs.move",
    ]


async def test_a_failure_is_kept_with_its_error(call_log) -> None:
    task_id = uuid4()
    await call_log.record(call(task_id=task_id, success=False))

    recorded = (await call_log.list_for_task(task_id))[0]
    assert not recorded.success
    assert recorded.error == "boom"


async def test_calls_belonging_to_another_task_are_not_returned(call_log) -> None:
    await call_log.record(call(task_id=uuid4()))
    assert await call_log.list_for_task(uuid4()) == []


async def test_a_credential_is_masked_on_the_way_into_the_store(call_log) -> None:
    task_id = uuid4()
    await call_log.record(
        ToolCallRecord(tool="api.send", success=True, task_id=task_id,
                       input_data={"api_key": "sk-live"})
    )

    stored = (await call_log.list_for_task(task_id))[0]
    assert stored.input_data == {"api_key": "***"}


async def test_the_interface_level_survives_a_round_trip(call_log) -> None:
    """A trace read months later must still say how the work reached the world."""
    from domain.computer.interfaces import InterfaceLevel

    task_id = uuid4()
    await call_log.record(
        ToolCallRecord(
            tool="computer.click",
            success=True,
            task_id=task_id,
            interface=InterfaceLevel.COMPUTER_USE,
        )
    )

    stored = await call_log.list_for_task(task_id)

    assert stored[0].interface is InterfaceLevel.COMPUTER_USE


async def test_a_call_that_says_nothing_about_its_interface_is_a_direct_call(
    call_log,
) -> None:
    from domain.computer.interfaces import InterfaceLevel

    task_id = uuid4()
    await call_log.record(call(task_id=task_id))

    assert (await call_log.list_for_task(task_id))[0].interface is InterfaceLevel.API


async def test_a_tool_intent_is_claimed_once_and_completed_in_place(call_log) -> None:
    from dataclasses import replace

    task_id = uuid4()
    intent = ToolCallRecord(
        tool="mail.send",
        success=False,
        task_id=task_id,
        call_id="provider-call-1",
        completed=False,
        input_data={"to": "person@example.com"},
    )

    assert await call_log.reserve(intent)
    assert not await call_log.reserve(intent)
    pending = await call_log.get_call(task_id, "provider-call-1")
    assert pending is not None and not pending.completed

    await call_log.complete(
        replace(intent, success=True, completed=True, output={"message_id": "m-1"})
    )
    completed = await call_log.get_call(task_id, "provider-call-1")

    assert completed is not None and completed.completed and completed.success
    assert completed.output == {"message_id": "m-1"}
    assert len(await call_log.list_for_task(task_id)) == 1
