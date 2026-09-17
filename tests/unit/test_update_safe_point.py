"""Phase 13: an update never restarts the program in the middle of an effect."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from application.employee_runtime.executor import Executor
from application.employee_runtime.transcript import Transcript
from domain.llm.models import ToolCallRequest
from domain.policies.risk import Effect
from domain.tasks.task import Task
from domain.tools.models import ToolResult
from infrastructure.computer.stop import FileStopSignal
from infrastructure.tasks.effects import InMemoryEffectGate
from infrastructure.tools.registry import InMemoryToolRegistry
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply, tool_reply
from tests.fakes.tools import FakeTool


async def test_a_held_gate_parks_the_next_effect_and_releasing_lets_it_through() -> None:
    gate = InMemoryEffectGate()
    gate.hold()
    entered = asyncio.Event()

    async def effect() -> None:
        async with gate.entering(uuid4(), "mail.send", "SEND"):
            entered.set()

    running = asyncio.create_task(effect())
    await asyncio.sleep(0.05)
    assert not entered.is_set(), "held: nothing new reaches the world"

    gate.release()
    await asyncio.wait_for(running, 1)
    assert entered.is_set()


async def test_waiting_for_idle_sees_an_effect_finish() -> None:
    gate = InMemoryEffectGate()
    finish = asyncio.Event()

    async def effect() -> None:
        async with gate.entering(uuid4(), "fs.delete", "DELETE"):
            await finish.wait()

    running = asyncio.create_task(effect())
    await asyncio.sleep(0.01)
    assert [item.tool for item in gate.in_flight()] == ["fs.delete"]
    assert await gate.wait_idle(0.05) is False

    finish.set()
    assert await gate.wait_idle(1.0) is True
    await running


def _run(tool: FakeTool, gate: InMemoryEffectGate, stop=None) -> asyncio.Task:
    task, employee = Task.create("Do it"), definition(tools=frozenset({tool.spec.name}))
    llm = FakeLLM(
        [tool_reply(ToolCallRequest(id="c1", name=tool.spec.name, arguments={})), reply("Done.")]
    )
    executor = Executor(llm, InMemoryToolRegistry([tool]), effects=gate, stop=stop)
    opening = Transcript(messages=Executor.opening_messages(task, employee, None, "be useful"))
    return asyncio.create_task(executor.run(task, employee, opening))


async def test_an_executor_waits_at_a_held_gate_for_a_write_but_not_for_a_read() -> None:
    gate = InMemoryEffectGate()
    gate.hold()
    write = FakeTool("fs.write", effect=Effect.WRITE, result=ToolResult.ok(written=True))
    read = FakeTool("fs.read", result=ToolResult.ok(text="hi"))

    reading = _run(read, gate)
    await asyncio.wait_for(reading, 2)
    assert read.calls == [{}], "a read is free to interrupt and is never held"

    writing = _run(write, gate)
    await asyncio.sleep(0.05)
    assert write.calls == [] and not writing.done()

    gate.release()
    await asyncio.wait_for(writing, 2)
    assert write.calls == [{}]


async def test_a_stop_pulled_while_an_effect_waited_at_the_gate_still_wins(
    tmp_path: Path,
) -> None:
    gate = InMemoryEffectGate()
    gate.hold()
    brake = FileStopSignal(tmp_path / "STOP")
    write = FakeTool("fs.write", effect=Effect.WRITE)

    writing = _run(write, gate, stop=brake)
    await asyncio.sleep(0.05)
    brake.engage("changed my mind")
    gate.release()
    outcome = await asyncio.wait_for(writing, 2)

    assert write.calls == []
    assert outcome.cancelled
