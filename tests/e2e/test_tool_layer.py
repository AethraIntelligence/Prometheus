"""An employee that does the work, not one that describes it.

Phase 4's Definition of Done, end to end on the real tools and a scripted model:
an employee sorts a folder using the tools its declaration lists, cannot touch
the ones it does not, and cannot destroy anything without a human saying yes.
"""

from __future__ import annotations

from pathlib import Path

from application.employee_runtime.approvals import ApprovalGate
from application.employee_runtime.executor import Executor
from application.employee_runtime.transcript import Transcript
from domain.llm.models import ToolCallRequest
from domain.tasks.task import Task
from infrastructure.persistence.tool_call_repository import InMemoryToolCallLog
from infrastructure.tools.builtin import build_registry
from tests.fakes.approvals import ScriptedApprovalService
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply, tool_reply

ORGANIZER_TOOLS = frozenset({"fs.list", "fs.read", "fs.move", "fs.write"})


def folder(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "acme-invoice-March.txt").write_text("INVOICE 0042\nAcme", encoding="utf-8")
    (workspace / "holiday.txt").write_text("Photos from the trip", encoding="utf-8")
    (workspace / "summary.md").write_text("the old summary", encoding="utf-8")
    return workspace


def opening(task: Task, employee) -> Transcript:
    return Transcript(
        messages=Executor.opening_messages(task, employee, None, "Sort the folder.")
    )


async def test_an_employee_sorts_a_folder_with_the_tools_it_declares(tmp_path: Path) -> None:
    workspace = folder(tmp_path)
    task = Task.create("Sort these documents into folders")
    employee = definition("organizer", tools=ORGANIZER_TOOLS)
    log = InMemoryToolCallLog()

    llm = FakeLLM(
        [
            tool_reply(ToolCallRequest(id="1", name="fs.list", arguments={"path": "."})),
            tool_reply(
                ToolCallRequest(
                    id="2", name="fs.read", arguments={"path": "acme-invoice-March.txt"}
                )
            ),
            tool_reply(
                ToolCallRequest(
                    id="3",
                    name="fs.move",
                    arguments={"source": "acme-invoice-March.txt", "destination": "invoices/"},
                )
            ),
            tool_reply(
                ToolCallRequest(
                    id="4",
                    name="fs.move",
                    arguments={"source": "holiday.txt", "destination": "personal/"},
                )
            ),
            reply("Sorted into invoices/ and personal/."),
        ]
    )

    outcome = await Executor(
        llm,
        build_registry(file_root=workspace, code_execution=False),
        approvals=ApprovalGate(ScriptedApprovalService.approving()),
        call_log=log,
    ).run(task, employee, opening(task, employee))

    assert outcome.finished
    assert (workspace / "invoices/acme-invoice-March.txt").exists()
    assert (workspace / "personal/holiday.txt").exists()
    # Nothing was destroyed. Moving after reading external document content is
    # still confirmed exactly, even though the move itself is reversible.
    assert (workspace / "summary.md").read_text() == "the old summary"
    assert all(call.success for call in await log.list_for_task(task.id))


async def test_overwriting_needs_a_yes_and_stops_without_one(tmp_path: Path) -> None:
    workspace = folder(tmp_path)
    task = Task.create("Write the summary")
    employee = definition("organizer", tools=ORGANIZER_TOOLS)

    llm = FakeLLM(
        [
            tool_reply(
                ToolCallRequest(
                    id="1",
                    name="fs.write",
                    arguments={"path": "summary.md", "content": "replaced"},
                )
            ),
            reply("I was not allowed to replace the existing summary."),
        ]
    )
    service = ScriptedApprovalService.rejecting()

    await Executor(
        llm,
        build_registry(file_root=workspace, code_execution=False),
        approvals=ApprovalGate(service),
    ).run(task, employee, opening(task, employee))

    assert (workspace / "summary.md").read_text() == "the old summary"
    assert "summary.md" in service.requests[0].action


async def test_the_same_write_goes_through_once_the_user_agrees(tmp_path: Path) -> None:
    workspace = folder(tmp_path)
    task = Task.create("Write the summary")
    employee = definition("organizer", tools=ORGANIZER_TOOLS)

    llm = FakeLLM(
        [
            tool_reply(
                ToolCallRequest(
                    id="1",
                    name="fs.write",
                    arguments={"path": "summary.md", "content": "replaced"},
                )
            ),
            reply("Replaced the summary."),
        ]
    )

    await Executor(
        llm,
        build_registry(file_root=workspace, code_execution=False),
        approvals=ApprovalGate(ScriptedApprovalService.approving()),
    ).run(task, employee, opening(task, employee))

    assert (workspace / "summary.md").read_text() == "replaced"


async def test_an_employee_is_shown_only_the_tools_it_may_use(tmp_path: Path) -> None:
    workspace = folder(tmp_path)
    task = Task.create("Sort these documents")
    employee = definition("organizer", tools=ORGANIZER_TOOLS)
    llm = FakeLLM([reply("Nothing to do.")])

    await Executor(
        llm, build_registry(file_root=workspace, code_execution=True)
    ).run(task, employee, opening(task, employee))

    offered = {spec.name for spec in llm.last_request.tools}
    assert offered == ORGANIZER_TOOLS
    assert "code.run" not in offered


async def test_a_tool_outside_the_declaration_is_refused_at_the_registry(tmp_path: Path) -> None:
    workspace = folder(tmp_path)
    task = Task.create("Run something")
    employee = definition("organizer", tools=ORGANIZER_TOOLS)

    llm = FakeLLM(
        [
            tool_reply(
                ToolCallRequest(id="1", name="code.run", arguments={"code": "print(1)"})
            ),
            reply("I do not have that tool."),
        ]
    )

    outcome = await Executor(
        llm,
        build_registry(file_root=workspace),
        approvals=ApprovalGate(ScriptedApprovalService.approving()),
    ).run(task, employee, opening(task, employee))

    assert "may not use" in outcome.transcript.observations[0].summary


async def test_the_workspace_is_the_boundary_even_when_the_model_asks_nicely(
    tmp_path: Path,
) -> None:
    workspace = folder(tmp_path)
    outside = tmp_path / "private.txt"
    outside.write_text("not for the employee", encoding="utf-8")
    task = Task.create("Read the private file")
    employee = definition("organizer", tools=ORGANIZER_TOOLS)

    llm = FakeLLM(
        [
            tool_reply(
                ToolCallRequest(
                    id="1", name="fs.read", arguments={"path": "../private.txt"}
                )
            ),
            reply("That file is outside my working directory."),
        ]
    )

    outcome = await Executor(
        llm,
        build_registry(file_root=workspace, code_execution=False),
        approvals=ApprovalGate(ScriptedApprovalService.approving()),
    ).run(task, employee, opening(task, employee))

    observation = outcome.transcript.observations[0]
    assert not observation.succeeded
    assert "outside the working directory" in observation.summary
