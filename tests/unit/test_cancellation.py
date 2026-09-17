"""Stopping a running task, and what is left behind when it stops."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from application.employee_runtime.executor import Executor
from application.employee_runtime.runtime import EmployeeRuntime, RuntimeDependencies
from application.employee_runtime.transcript import Transcript
from domain.llm.models import ToolCallRequest
from domain.tasks.task import Task, TaskStatus
from domain.tools.models import ToolResult
from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
from infrastructure.progress.broadcaster import InMemoryProgressBroadcaster
from infrastructure.tasks.cancellation import InMemoryCancellations
from infrastructure.tools.registry import InMemoryToolRegistry
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply, tool_reply
from tests.fakes.tools import FakeTool


def opening(task: Task, employee) -> Transcript:
    return Transcript(messages=Executor.opening_messages(task, employee, None, "be useful"))


class CancelAfter:
    """A signal that turns on once a given number of questions have been asked.

    Cancelling from another coroutine would work too, but it would make the test
    depend on scheduling; the point being checked is *where* the loop looks.
    """

    def __init__(self, asks: int) -> None:
        self._remaining = asks
        self.asked = 0

    def is_cancelled(self, task_id: UUID) -> bool:
        self.asked += 1
        self._remaining -= 1
        return self._remaining < 0


# --- The loop -----------------------------------------------------------------


async def test_a_cancelled_run_stops_between_steps() -> None:
    task = Task.create("Keep going forever")
    employee = definition()
    llm = FakeLLM([reply("first"), reply("second")])

    outcome = await Executor(
        llm, InMemoryToolRegistry(), cancellation=CancelAfter(0)
    ).run(task, employee, opening(task, employee))

    assert outcome.cancelled
    assert outcome.finished
    assert llm.call_count == 0, "cancelled before it spent anything"


async def test_a_paused_run_spends_nothing_until_it_is_resumed() -> None:
    task = Task.create("Wait safely")
    employee = definition()
    llm = FakeLLM([reply("Finished after resume.")])
    cancellations = InMemoryCancellations()
    cancellations.pause(task.id)
    statuses: list[TaskStatus] = []

    async def status(value: TaskStatus) -> None:
        statuses.append(value)

    run = asyncio.create_task(
        Executor(llm, InMemoryToolRegistry(), cancellation=cancellations).run(
            task, employee, opening(task, employee), on_status=status
        )
    )
    await asyncio.sleep(0)

    assert statuses == [TaskStatus.PAUSED]
    assert llm.call_count == 0
    assert not run.done()

    cancellations.resume(task.id)
    outcome = await run
    assert statuses[-1] is TaskStatus.RUNNING
    assert outcome.answer == "Finished after resume."


async def test_the_work_already_done_is_kept() -> None:
    task = Task.create("Read the file, then keep going")
    employee = definition(tools=frozenset({"fs.read"}))
    tool = FakeTool("fs.read", result=ToolResult.ok(text="hello"))
    registry = InMemoryToolRegistry([tool])
    llm = FakeLLM(
        [
            tool_reply(ToolCallRequest(id="c1", name="fs.read", arguments={"path": "a.txt"})),
            reply("never reached"),
        ]
    )
    # One question at the top of the first step, one before the tool call, and
    # the third - at the top of the second step - is the one that stops it.
    outcome = await Executor(llm, registry, cancellation=CancelAfter(2)).run(
        task, employee, opening(task, employee)
    )

    assert outcome.cancelled
    assert [o.summary for o in outcome.transcript.observations] == [
        "fs.read returned: {'text': 'hello'}"
    ]
    assert tool.calls == [{"path": "a.txt"}]


async def test_a_run_cancelled_mid_step_makes_no_further_tool_calls() -> None:
    task = Task.create("Read two files")
    employee = definition(tools=frozenset({"fs.read"}))
    tool = FakeTool("fs.read", result=ToolResult.ok(text="hello"))
    llm = FakeLLM(
        [
            tool_reply(
                ToolCallRequest(id="c1", name="fs.read", arguments={"path": "a.txt"}),
                ToolCallRequest(id="c2", name="fs.read", arguments={"path": "b.txt"}),
            )
        ]
    )
    # Asked at the top of the step, then before each of the two calls; the
    # second call is the one that finds the signal on.
    outcome = await Executor(
        llm, InMemoryToolRegistry([tool]), cancellation=CancelAfter(2)
    ).run(task, employee, opening(task, employee))

    assert outcome.cancelled
    assert tool.calls == [{"path": "a.txt"}], "the second call was never made"


# --- The task -----------------------------------------------------------------


async def test_a_cancelled_task_ends_cancelled_and_is_never_verified() -> None:
    """A deliberate stop is not a failed verification, and must not read as one."""
    tasks = InMemoryTaskRepository()
    task = Task.create("Do something long")
    await tasks.save(task)
    employee = definition()
    cancellations = InMemoryCancellations()
    cancellations.cancel(task.id, "changed my mind")

    class ExplodingVerifier:
        async def verify(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("a cancelled run has nothing to verify")

    class StubPlanner:
        async def plan(self, *args, **kwargs):
            from domain.tasks.plan import TaskPlan

            return TaskPlan.of("Look at it")

    progress = InMemoryProgressBroadcaster()
    runtime = EmployeeRuntime(
        employee,
        RuntimeDependencies(
            planner=StubPlanner(),  # type: ignore[arg-type]
            executor=Executor(
                FakeLLM([reply("unused")]),
                InMemoryToolRegistry(),
                cancellation=cancellations,
            ),
            verifier=ExplodingVerifier(),  # type: ignore[arg-type]
            tasks=tasks,
            tools=InMemoryToolRegistry(),
            limits=employee.limits,
            progress=progress,
        ),
    )

    await runtime.run(task)

    stored = await tasks.get(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.CANCELLED
    assert stored.result is not None, "what was done up to the stop is kept"
    assert any(
        event.payload.get("status") == "CANCELLED" for event in progress.recent(task.id)
    )


def test_the_registry_forgets_a_request_once_it_is_cleared() -> None:
    cancellations = InMemoryCancellations()
    task_id = uuid4()

    assert not cancellations.is_cancelled(task_id)
    cancellations.cancel(task_id, "stop")
    assert cancellations.is_cancelled(task_id)
    assert cancellations.reason_for(task_id) == "stop"

    cancellations.clear(task_id)
    assert not cancellations.is_cancelled(task_id)
    assert cancellations.reason_for(task_id) == ""


@pytest.mark.asyncio
async def test_a_pause_waits_until_resume_and_cancel_also_releases_it() -> None:
    cancellations = InMemoryCancellations()
    task_id = uuid4()
    cancellations.pause(task_id)

    parked = asyncio.create_task(cancellations.wait_until_resumed(task_id))
    await asyncio.sleep(0)
    assert not parked.done()

    cancellations.resume(task_id)
    await parked
    assert not cancellations.is_paused(task_id)

    cancellations.pause(task_id)
    parked = asyncio.create_task(cancellations.wait_until_resumed(task_id))
    cancellations.cancel(task_id, "stop instead")
    await parked
    assert cancellations.is_cancelled(task_id)
