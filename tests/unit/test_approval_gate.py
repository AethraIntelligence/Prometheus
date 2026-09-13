"""The brake: which actions wait for a person, and what happens when nobody answers."""

from __future__ import annotations

import pytest

from application.employee_runtime.approvals import ApprovalGate
from domain.approvals.gate import RiskAssessment, assess_call, describe
from domain.approvals.models import ApprovalState
from domain.policies.models import Decision, RiskLevel
from domain.policies.risk import Effect
from domain.tasks.task import Task, TaskStatus
from domain.tools.models import ToolResult, ToolSpec
from domain.tools.schema import Param
from infrastructure.persistence.audit_repository import InMemoryAuditLog
from tests.fakes.approvals import ScriptedApprovalService
from tests.fakes.employees import definition
from tests.fakes.tools import FakeTool


def spec(**kwargs) -> ToolSpec:
    return ToolSpec.of("fs.write", "Write a file.", Param("path"), **kwargs)


# --- The rule ------------------------------------------------------------------


def test_a_low_risk_action_just_happens() -> None:
    assert assess_call(spec()).decision is Decision.ALLOW


def test_a_high_risk_action_waits_for_a_person() -> None:
    assert assess_call(spec(risk_level=RiskLevel.HIGH)).decision is Decision.REQUIRE_APPROVAL


def test_an_irreversible_tool_always_waits_whatever_it_says_about_the_call() -> None:
    """Declaring a tool irreversible is the whole point; it cannot talk itself down."""
    decision = assess_call(
        spec(reversible=False), RiskAssessment(RiskLevel.LOW, "looks harmless")
    )

    assert decision.decision is Decision.REQUIRE_APPROVAL


def test_a_reversible_tool_knows_more_about_the_call_than_its_spec_does() -> None:
    declared = spec(risk_level=RiskLevel.MEDIUM)

    assert assess_call(declared, RiskAssessment(RiskLevel.HIGH, "would overwrite")).decision is (
        Decision.REQUIRE_APPROVAL
    )
    assert assess_call(declared, RiskAssessment(RiskLevel.LOW, "new file")).decision is (
        Decision.ALLOW
    )


def test_the_reason_shown_to_the_user_comes_from_the_tool() -> None:
    decision = assess_call(spec(), RiskAssessment(RiskLevel.HIGH, "Overwrite notes.txt"))
    assert decision.reason == "Overwrite notes.txt"


def test_a_long_argument_is_trimmed_before_it_is_shown() -> None:
    line = describe(spec(), {"path": "a.txt", "content": "x" * 500})
    assert len(line) < 300
    assert "..." in line


# --- The gate ------------------------------------------------------------------


class RiskyTool(FakeTool):
    """A tool that always says this call is dangerous."""

    def assess(self, input_data: dict) -> RiskAssessment:
        return RiskAssessment(RiskLevel.HIGH, "this would destroy something")


@pytest.fixture
def task() -> Task:
    return Task.create("Tidy the folder")


async def test_an_approved_action_proceeds(task: Task) -> None:
    service = ScriptedApprovalService.approving()
    tool = RiskyTool("fs.write", result=ToolResult.ok(written=True))

    outcome = await ApprovalGate(service).check(tool, {"path": "a"}, task, definition())

    assert outcome.allowed
    assert service.requests[0].task_id == task.id
    assert service.requests[0].risk_level is RiskLevel.HIGH


async def test_a_rejected_action_does_not_happen_and_says_why(task: Task) -> None:
    outcome = await ApprovalGate(ScriptedApprovalService.rejecting()).check(
        RiskyTool("fs.write"), {"path": "a"}, task, definition()
    )

    assert not outcome.allowed
    assert "did not approve" in outcome.reason


async def test_with_no_approver_configured_nothing_irreversible_happens(task: Task) -> None:
    """The default for an action nobody confirmed is not to do it."""
    outcome = await ApprovalGate(None).check(
        RiskyTool("fs.write"), {"path": "a"}, task, definition()
    )

    assert not outcome.allowed
    assert "no approver is configured" in outcome.reason


async def test_a_harmless_call_is_never_put_to_the_user(task: Task) -> None:
    service = ScriptedApprovalService.approving()

    outcome = await ApprovalGate(service).check(
        FakeTool("fs.read"), {"path": "a"}, task, definition()
    )

    assert outcome.allowed
    assert service.requests == [], "a question nobody needed to answer trains them to click yes"


async def test_the_stored_request_carries_no_credential(task: Task) -> None:
    service = ScriptedApprovalService.approving()

    await ApprovalGate(service).check(
        RiskyTool("api.send"), {"url": "https://x", "api_key": "sk-secret"}, task, definition()
    )

    assert service.requests[0].payload["api_key"] == "***"
    assert "sk-secret" not in str(service.requests[0].payload)


async def test_the_decision_records_who_asked(task: Task) -> None:
    service = ScriptedApprovalService.approving()
    employee = definition("organizer")

    await ApprovalGate(service).check(RiskyTool("fs.move"), {}, task, employee)

    assert service.requests[0].requested_by_employee_id == employee.id


def test_an_approval_carries_its_decision_and_when_it_was_made() -> None:
    from domain.approvals.models import Approval, ApprovalRequest

    approval = Approval(request=ApprovalRequest.create(Task.create("x").id, "fs.write(...)"))
    assert approval.is_pending

    resolved = approval.resolve(ApprovalState.APPROVED, comment="fine")

    assert resolved.state is ApprovalState.APPROVED
    assert resolved.resolved_at is not None
    assert resolved.id == approval.id


# --- The gate asks the policy engine, not only the threshold (Phase 10) --------


async def test_a_denied_action_is_refused_without_asking_anybody() -> None:
    """A declared restriction is not a question. Putting it to the user would
    turn the declaration into a suggestion."""
    service = ScriptedApprovalService.approving()
    gate = ApprovalGate(service)
    tool = FakeTool("fs.write", effect=Effect.WRITE)

    outcome = await gate.check(
        tool,
        {"path": "notes.md"},
        Task.create("write something"),
        definition(tools={"fs.write"}, policies={"read_only"}),
    )

    assert outcome.allowed is False
    assert service.requests == []
    assert "read-only" in outcome.reason


async def test_a_refusal_is_written_to_the_audit_because_nothing_else_records_it() -> None:
    """The tool never ran, so there is no tool call to account for. A denial
    that leaves no trace is the one an audit exists to show."""
    audit = InMemoryAuditLog()
    gate = ApprovalGate(ScriptedApprovalService.rejecting(), audit=audit)

    await gate.check(
        FakeTool("code.run", effect=Effect.EXECUTE, reversible=False),
        {"code": "print(1)"},
        Task.create("compute"),
        definition(tools={"code.run"}),
    )

    assert [record.result for record in audit.records] == ["DENIED"]


async def test_an_approved_action_is_not_audited_twice() -> None:
    """The executor audits what actually ran. A second line here would make
    every approved action appear twice in the one place people count them."""
    audit = InMemoryAuditLog()
    gate = ApprovalGate(ScriptedApprovalService.approving(), audit=audit)

    outcome = await gate.check(
        FakeTool("code.run", effect=Effect.EXECUTE, reversible=False),
        {"code": "print(1)"},
        Task.create("compute"),
        definition(tools={"code.run"}),
    )

    assert outcome.allowed is True
    assert audit.records == []


async def test_the_task_is_parked_while_a_person_is_being_asked() -> None:
    """`prometheus tasks` should say what a run is doing, not show it as RUNNING
    with nothing happening."""
    seen: list[TaskStatus] = []

    async def status(value: TaskStatus) -> None:
        seen.append(value)

    gate = ApprovalGate(ScriptedApprovalService.approving())
    await gate.check(
        FakeTool("code.run", effect=Effect.EXECUTE, reversible=False),
        {"code": "print(1)"},
        Task.create("compute"),
        definition(tools={"code.run"}),
        status=status,
    )

    assert seen == [TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.RUNNING]


async def test_the_task_stops_waiting_even_if_asking_blew_up() -> None:
    """A task left in WAITING_FOR_APPROVAL would be resumable forever, waiting
    on a person nobody asked."""
    seen: list[TaskStatus] = []

    class Broken:
        async def request(self, action):
            raise RuntimeError("the browser went away")

        async def resolve(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

    gate = ApprovalGate(Broken())
    with pytest.raises(RuntimeError):
        await gate.check(
            FakeTool("code.run", effect=Effect.EXECUTE, reversible=False),
            {"code": "print(1)"},
            Task.create("compute"),
            definition(tools={"code.run"}),
            status=lambda value: _record(seen, value),
        )

    assert seen[-1] is TaskStatus.RUNNING


async def _record(seen: list[TaskStatus], value: TaskStatus) -> None:
    seen.append(value)


# --- What the request said about asking ---------------------------------------


async def test_a_request_set_to_refuse_is_refused_without_asking(task: Task) -> None:
    from domain.workforce.directions import ApprovalChoice, Directions, given

    service = ScriptedApprovalService.approving()
    with given(Directions(approvals=ApprovalChoice.DENY)):
        outcome = await ApprovalGate(service).check(
            RiskyTool("fs.write"), {"path": "a"}, task, definition()
        )

    assert not outcome.allowed
    assert service.requests == [], "refusing in advance needs nobody to answer"
