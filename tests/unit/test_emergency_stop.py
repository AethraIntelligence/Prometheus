"""The emergency stop: a record that survives restarts and an effect that cannot slip past."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from application.employee_runtime.approvals import ApprovalGate
from application.employee_runtime.executor import Executor
from application.employee_runtime.transcript import Transcript
from domain.approvals.gate import RiskAssessment
from domain.approvals.models import ApprovalRequest, ApprovalState
from domain.errors import WorkStoppedError
from domain.llm.models import ToolCallRequest
from domain.policies.models import RiskLevel
from domain.safety import emergency
from domain.tasks.task import Task
from domain.tools.models import ToolResult
from infrastructure.computer.stop import FileStopSignal
from infrastructure.persistence.audit_repository import InMemoryAuditLog
from infrastructure.persistence.tool_call_repository import InMemoryToolCallLog
from infrastructure.tools.registry import InMemoryToolRegistry
from tests.fakes.approvals import ScriptedApprovalService
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply, tool_reply
from tests.fakes.tools import FakeTool

# --- The record -----------------------------------------------------------------


def test_a_plain_text_record_from_an_earlier_version_still_stops_with_its_reason() -> None:
    state = emergency.parse("wrong window")

    assert state.engaged
    assert state.reason == "wrong window"
    assert not state.unreadable


def test_the_record_round_trips() -> None:
    at = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
    written = emergency.engaged("enough", "person", at)

    read = emergency.parse(emergency.render(written))

    assert read == written


@pytest.mark.parametrize(
    "text",
    ["{not json", json.dumps({"version": 99, "reason": "x"}), json.dumps({"reason": "x"})],
)
def test_a_record_this_version_cannot_read_is_engaged_never_released(text: str) -> None:
    state = emergency.parse(text)

    assert state.engaged
    assert state.unreadable


def test_the_file_holds_across_a_new_reader_and_says_who(tmp_path: Path) -> None:
    FileStopSignal(tmp_path / "STOP").engage("all of it", by="cli")

    state = FileStopSignal(tmp_path / "STOP").state()

    assert state.engaged
    assert state.engaged_by == "cli"
    assert state.engaged_at is not None
    assert not list(tmp_path.glob(".STOP.*.tmp")), "the temporary file must not be left"


def test_a_failed_write_leaves_the_previous_record_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk while engaging must not read as released, nor corrupt an earlier stop."""
    signal = FileStopSignal(tmp_path / "STOP")
    signal.engage("first")

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("infrastructure.computer.stop.os.fsync", full)
    with pytest.raises(OSError):
        signal.engage("second")

    assert FileStopSignal(tmp_path / "STOP").state().reason == "first"


# --- The approval/effect boundary -----------------------------------------------


class DangerousTool(FakeTool):
    def assess(self, input_data: dict) -> RiskAssessment:
        return RiskAssessment(RiskLevel.HIGH, "would overwrite notes.txt")


class StopWhileApproving(ScriptedApprovalService):
    """A person approves in the same instant somebody else pulls the brake."""

    def __init__(self, brake: FileStopSignal) -> None:
        super().__init__(default=ApprovalState.APPROVED)
        self._brake = brake

    async def request(self, action: ApprovalRequest) -> ApprovalState:
        self._brake.engage("stopped during the approval", by="second person")
        return await super().request(action)


def _opening(task: Task, employee) -> Transcript:
    return Transcript(messages=Executor.opening_messages(task, employee, None, "be useful"))


async def test_an_approval_cannot_carry_an_effect_past_a_stop_set_while_it_was_asked(
    tmp_path: Path,
) -> None:
    """The Definition of Done of Phase 13's stop, as a test."""
    brake = FileStopSignal(tmp_path / "STOP")
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    tool = DangerousTool("fs.write", result=ToolResult.ok(written=True))
    service = StopWhileApproving(brake)
    audit = InMemoryAuditLog()
    calls = InMemoryToolCallLog()

    outcome = await Executor(
        FakeLLM(
            [
                tool_reply(ToolCallRequest(id="c1", name="fs.write", arguments={"path": "n"})),
                reply("Done."),
            ]
        ),
        InMemoryToolRegistry([tool]),
        approvals=ApprovalGate(service, audit=audit),
        audit=audit,
        call_log=calls,
        stop=brake,
    ).run(task, employee, _opening(task, employee))

    assert len(service.requests) == 1, "the question was asked and answered yes"
    assert tool.calls == [], "and still nothing happened"
    assert outcome.cancelled, "the run ends at the next boundary"
    assert "not performed" in outcome.transcript.observations[0].summary
    [ledger] = await calls.list_for_task(task.id)
    assert ledger.completed and not ledger.success, "not left as an outcome-unknown reservation"
    denied = [r for r in audit.records if r.details.get("policy_source") == "emergency_stop"]
    assert [r.result for r in denied] == ["DENIED"]


async def test_a_stopped_machine_asks_nobody_and_calls_nothing(tmp_path: Path) -> None:
    brake = FileStopSignal(tmp_path / "STOP")
    brake.engage("before the run")
    task, employee = Task.create("Write it"), definition(tools=frozenset({"fs.write"}))
    tool = DangerousTool("fs.write")
    service = ScriptedApprovalService.approving()
    llm = FakeLLM([tool_reply(ToolCallRequest(id="c1", name="fs.write", arguments={}))])

    outcome = await Executor(
        llm, InMemoryToolRegistry([tool]), approvals=ApprovalGate(service), stop=brake
    ).run(task, employee, _opening(task, employee))

    assert outcome.cancelled
    assert service.requests == [] and tool.calls == []
    assert llm.requests == [], "not even the model is asked while stopped"


# --- Refusing new work ---------------------------------------------------------------


async def test_the_runner_records_no_new_task_while_stopped(tmp_path: Path) -> None:
    from application.task_runner import TaskRunner
    from infrastructure.persistence.assignment_repository import InMemoryAssignmentRepository
    from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
    from tests.fakes.workforce import FakeRegistry

    brake = FileStopSignal(tmp_path / "STOP")
    brake.engage("no")
    tasks = InMemoryTaskRepository()
    runner = TaskRunner(
        tasks=tasks,
        assignments=InMemoryAssignmentRepository(),
        registry=FakeRegistry(definition(name="organizer")),
        build_runtime=None,  # type: ignore[arg-type]
        stop=brake,
    )

    with pytest.raises(WorkStoppedError, match="Resume work"):
        await runner.submit("anything", "organizer")
    assert await tasks.list_recent() == []


async def test_a_stopped_scheduler_fires_nothing(tmp_path: Path) -> None:
    from application.scheduling.scheduler import Scheduler

    brake = FileStopSignal(tmp_path / "STOP")
    brake.engage("no")

    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"a stopped scheduler must not reach {name}")

    scheduler = Scheduler(
        manager=Untouchable(),  # type: ignore[arg-type]
        schedules=Untouchable(),  # type: ignore[arg-type]
        events=Untouchable(),  # type: ignore[arg-type]
        stop=brake,
    )

    assert await scheduler.tick() == ()


def test_the_record_the_desktop_shell_writes_reads_as_an_engaged_stop() -> None:
    """The menu's fallback writes the record in Rust; this is the contract between the two."""
    import re

    source = (Path(__file__).resolve().parents[2] / "desktop/src-tauri/src/brake.rs").read_text()
    reason = re.search(r'const REASON: &str = "([^"]+)";', source)
    template = re.search(r'let record = format!\(\s*"(.+?)"\s*\);', source, re.DOTALL)
    assert reason and template, "the shell's record format moved; update this contract"
    written = (
        template.group(1).replace("{{", "{").replace("}}", "}").replace('\\"', '"')
        .replace("{REASON}", reason.group(1))
    )

    state = emergency.parse(written)

    assert state.engaged and not state.unreadable
    assert state.reason == reason.group(1)
    assert state.engaged_by == "menu"


# --- Supported platforms ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "release", "machine", "supported"),
    [
        ("Darwin", "13.0", "arm64", True),
        ("Darwin", "27.0", "x86_64", True),
        ("Darwin", "12.7.4", "arm64", False),
        ("Windows", "10.0.19045", "AMD64", True),
        ("Windows", "10.0.19044", "AMD64", False),
        ("Windows", "10.0.22631", "ARM64", False),
        ("Linux", "2.35", "x86_64", True),
        ("Linux", "2.31", "x86_64", False),
        ("Linux", "", "x86_64", False),
        ("Linux", "2.39", "aarch64", False),
        ("FreeBSD", "14.0", "amd64", False),
    ],
)
def test_the_supported_matrix_is_what_the_release_notes_say(system, release, machine, supported):
    from domain.safety.platform import check

    verdict = check(system, release, machine)

    assert verdict.supported is supported
    assert supported or verdict.message


def test_this_machine_is_asked_for_the_facts_the_check_needs() -> None:
    from infrastructure.runtime.platform import this_machine, verdict

    system, _, machine = this_machine()

    assert system and machine
    assert verdict().supported in (True, False)


# --- The validation suite's declared person ----------------------------------------------


def _request(tool: str) -> ApprovalRequest:
    from domain.approvals.models import ApprovalRequest as Request

    return Request.create(task_id=Task.create("x").id, action=f"{tool}()", tool=tool)


def test_a_scenario_can_pull_the_stop_as_it_approves_and_releases_only_its_own(
    tmp_path: Path,
) -> None:
    from infrastructure.validation.approver import DeclaredApprover

    brake = FileStopSignal(tmp_path / "STOP")
    approver = DeclaredApprover(brake=brake)

    with approver.answering(frozenset({"fs.write"}), frozenset({"fs.write"})):
        assert approver.confirm(_request("fs.read")) is False
        assert not brake.engaged()
        assert approver.confirm(_request("fs.write")) is True
        assert brake.engaged() and "validation" in brake.reason
    assert not brake.engaged(), "the run's own stop is lifted when the run ends"

    brake.engage("a person's stop")
    with approver.answering(frozenset(), frozenset()):
        pass
    assert brake.engaged(), "a stop somebody else set is never lifted by the suite"
