"""The tool path through the executor: permission, approval, accounting."""

from __future__ import annotations

from dataclasses import replace

from application.employee_runtime.approvals import ApprovalGate
from application.employee_runtime.executor import Executor
from application.employee_runtime.transcript import Transcript
from domain.approvals.gate import RiskAssessment
from domain.llm.models import Message, ToolCallRequest
from domain.policies.models import RiskLevel
from domain.tasks.task import Task
from domain.tools.models import ToolResult
from domain.tools.telemetry import ToolCallRecord
from infrastructure.persistence.tool_call_repository import InMemoryToolCallLog
from infrastructure.tools.registry import InMemoryToolRegistry
from tests.fakes.approvals import ScriptedApprovalService
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply, tool_reply
from tests.fakes.tools import FakeTool


class DangerousTool(FakeTool):
    def assess(self, input_data: dict) -> RiskAssessment:
        return RiskAssessment(RiskLevel.HIGH, "would overwrite notes.txt")


def opening(task: Task, employee) -> Transcript:
    return Transcript(messages=Executor.opening_messages(task, employee, None, "be useful"))


def one_call(name: str, **arguments) -> FakeLLM:
    return FakeLLM(
        [
            tool_reply(ToolCallRequest(id="c1", name=name, arguments=arguments)),
            reply("Done."),
        ]
    )


async def test_an_approved_call_reaches_the_tool() -> None:
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    tool = DangerousTool("fs.write", result=ToolResult.ok(written=True))

    await Executor(
        one_call("fs.write", path="notes.txt"),
        InMemoryToolRegistry([tool]),
        approvals=ApprovalGate(ScriptedApprovalService.approving()),
    ).run(task, employee, opening(task, employee))

    assert tool.calls == [{"path": "notes.txt"}]


async def test_a_rejected_call_never_reaches_the_tool() -> None:
    """The DoD of this phase: an irreversible action without a yes cannot happen."""
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    tool = DangerousTool("fs.write")

    outcome = await Executor(
        one_call("fs.write", path="notes.txt"),
        InMemoryToolRegistry([tool]),
        approvals=ApprovalGate(ScriptedApprovalService.rejecting()),
    ).run(task, employee, opening(task, employee))

    assert tool.calls == []
    assert "did not approve" in outcome.transcript.observations[0].summary


async def test_without_an_approval_service_a_dangerous_call_is_refused() -> None:
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    tool = DangerousTool("fs.write")

    await Executor(
        one_call("fs.write", path="notes.txt"), InMemoryToolRegistry([tool])
    ).run(task, employee, opening(task, employee))

    assert tool.calls == []


async def test_a_tool_the_employee_does_not_have_is_never_put_to_the_user() -> None:
    """Permission is checked before approval: refusing costs nothing, asking costs a person."""
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.read"}))
    service = ScriptedApprovalService.approving()

    outcome = await Executor(
        one_call("fs.write", path="notes.txt"),
        InMemoryToolRegistry([DangerousTool("fs.write")]),
        approvals=ApprovalGate(service),
    ).run(task, employee, opening(task, employee))

    assert service.requests == []
    assert "may not use" in outcome.transcript.observations[0].summary


async def test_every_call_is_accounted_for() -> None:
    task, employee = Task.create("Read it"), definition(tools=frozenset({"fs.read"}))
    log = InMemoryToolCallLog()

    await Executor(
        one_call("fs.read", path="a.txt"),
        InMemoryToolRegistry([FakeTool("fs.read", result=ToolResult.ok(text="hi"))]),
        call_log=log,
    ).run(task, employee, opening(task, employee))

    recorded = await log.list_for_task(task.id)
    assert [call.tool for call in recorded] == ["fs.read"]
    assert recorded[0].success


async def test_a_refused_call_is_recorded_too() -> None:
    # Otherwise the trace shows an employee that simply stopped, with no reason.
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    log = InMemoryToolCallLog()

    await Executor(
        one_call("fs.write", path="notes.txt"),
        InMemoryToolRegistry([DangerousTool("fs.write")]),
        approvals=ApprovalGate(ScriptedApprovalService.rejecting()),
        call_log=log,
    ).run(task, employee, opening(task, employee))

    recorded = await log.list_for_task(task.id)
    assert [(call.tool, call.success) for call in recorded] == [("fs.write", False)]


async def test_a_credential_in_an_argument_never_enters_the_transcript() -> None:
    """The transcript is persisted and sent back to the model on the next step."""
    task, employee = Task.create("Send it"), definition(tools=frozenset({"api.send"}))
    log = InMemoryToolCallLog()

    outcome = await Executor(
        one_call("api.send", url="https://x", api_key="sk-live-secret"),
        InMemoryToolRegistry([FakeTool("api.send", result=ToolResult.ok(token="t-1"))]),
        call_log=log,
    ).run(task, employee, opening(task, employee))

    observation = outcome.transcript.observations[0]
    assert "sk-live-secret" not in str(observation.to_dict())
    assert observation.details["arguments"]["api_key"] == "***"
    assert "sk-live-secret" not in str((await log.list_for_task(task.id))[0].input_data)


async def test_a_broken_telemetry_log_does_not_fail_the_task() -> None:
    class BrokenLog:
        async def record(self, call) -> None:
            raise RuntimeError("disk is full")

        async def list_for_task(self, task_id):  # pragma: no cover - never reached
            return []

    task, employee = Task.create("Read it"), definition(tools=frozenset({"fs.read"}))

    outcome = await Executor(
        one_call("fs.read", path="a.txt"),
        InMemoryToolRegistry([FakeTool("fs.read")]),
        call_log=BrokenLog(),
    ).run(task, employee, opening(task, employee))

    assert outcome.finished


async def test_a_completed_pending_call_is_replayed_without_running_the_tool() -> None:
    """A crash after the action but before transcript save cannot repeat it."""
    task, employee = Task.create("Send it"), definition(tools=frozenset({"api.send"}))
    request = ToolCallRequest(id="durable-1", name="api.send", arguments={"message": "hi"})
    transcript = opening(task, employee).with_message(
        Message.assistant("", tool_calls=(request,))
    )
    log = InMemoryToolCallLog()
    intent = ToolCallRecord(
        tool="api.send",
        success=False,
        task_id=task.id,
        call_id=request.id,
        completed=False,
        input_data=request.arguments,
    )
    assert await log.reserve(intent)
    await log.complete(
        replace(intent, success=True, completed=True, output={"message_id": "m-1"})
    )
    tool = FakeTool("api.send", result=ToolResult.ok(message_id="m-2"))

    outcome = await Executor(
        FakeLLM([reply("Done.")]), InMemoryToolRegistry([tool]), call_log=log
    ).run(task, employee, transcript)

    assert tool.calls == []
    assert "m-1" in outcome.transcript.observations[0].summary


async def test_an_uncertain_pending_call_is_not_repeated() -> None:
    task, employee = Task.create("Send it"), definition(tools=frozenset({"api.send"}))
    request = ToolCallRequest(id="durable-1", name="api.send", arguments={"message": "hi"})
    transcript = opening(task, employee).with_message(Message.assistant("", (request,)))
    log = InMemoryToolCallLog()
    await log.reserve(
        ToolCallRecord(
            tool="api.send",
            success=False,
            task_id=task.id,
            call_id=request.id,
            completed=False,
            input_data=request.arguments,
        )
    )
    tool = FakeTool("api.send")

    outcome = await Executor(
        FakeLLM([reply("I could not safely confirm it.")]),
        InMemoryToolRegistry([tool]),
        call_log=log,
    ).run(task, employee, transcript)

    assert tool.calls == []
    assert "outcome is unknown" in outcome.transcript.observations[0].summary


# --- A tool that keeps being refused ------------------------------------------
#
# Phase 11 finding 2: an analyst forbidden `code.run` called it fifteen times
# and spent the whole step budget being told no. The refusal text says not to;
# text is a suggestion, so the tool is withdrawn instead.


def repeated(name: str, times: int) -> FakeLLM:
    """A model that asks for the same tool over and over, then gives up."""
    return FakeLLM(
        [
            *(
                tool_reply(ToolCallRequest(id=f"c{i}", name=name, arguments={"path": "x"}))
                for i in range(times)
            ),
            reply("I could not do it."),
        ]
    )


async def test_a_tool_refused_twice_stops_being_offered() -> None:
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    tool = DangerousTool("fs.write")
    llm = repeated("fs.write", 5)

    outcome = await Executor(
        llm,
        InMemoryToolRegistry([tool]),
        approvals=ApprovalGate(ScriptedApprovalService.rejecting()),
    ).run(task, employee, opening(task, employee))

    assert tool.calls == []
    offered = [[spec.name for spec in request.tools] for request in llm.requests]
    assert offered[0] == ["fs.write"], "it is offered until it has been refused twice"
    assert offered[1] == ["fs.write"]
    assert all(names == [] for names in offered[2:]), "and never again in this task"
    assert "no longer available" in outcome.transcript.observations[1].summary


async def test_a_tool_that_merely_fails_keeps_being_offered() -> None:
    """Refused is not failed: a timeout may not happen twice, a policy will."""
    task, employee = Task.create("Read it"), definition(tools=frozenset({"fs.read"}))
    tool = FakeTool("fs.read", result=ToolResult.failure("the page timed out"))
    llm = repeated("fs.read", 4)

    await Executor(llm, InMemoryToolRegistry([tool])).run(
        task, employee, opening(task, employee)
    )

    assert len(tool.calls) == 4
    assert all([spec.name for spec in request.tools] == ["fs.read"] for request in llm.requests)


async def test_a_resumed_run_withholds_what_the_first_one_did() -> None:
    """The count is derived from the transcript, so it survives the process."""
    from domain.tools.refusals import withheld

    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    first = await Executor(
        repeated("fs.write", 2),
        InMemoryToolRegistry([DangerousTool("fs.write")]),
        approvals=ApprovalGate(ScriptedApprovalService.rejecting()),
    ).run(task, employee, opening(task, employee))

    carried = Transcript.from_state(first.transcript.to_state())
    assert withheld(carried.observations) == frozenset({"fs.write"})
