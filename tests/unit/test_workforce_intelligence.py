"""Phase 11: roles with verifiable readiness, contracts, decisions and records."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from application.prometheus.delegation import CapabilityDelegator
from application.prometheus.supervisor import Recovery, Supervisor
from application.workforce.readiness import ReadinessService
from domain.capabilities.models import Capability, CapabilityRequirement
from domain.employees.contract import (
    UNDECLARED,
    EvidenceKind,
    FailureKind,
    WorkContract,
    WorkProduct,
)
from domain.errors import ConfigurationError, DelegationError
from domain.integrations.models import Integration, IntegrationStatus
from domain.policies.risk import Effect
from domain.tasks.task import Task, TaskError, TaskResult, TaskStatus
from domain.workflows.suggestion import Pattern, StepShape, reconcile
from domain.workforce.acceptance import Acceptance, AcceptanceCode, accept
from domain.workforce.decision import Decision, RejectionCode, SelectionCode
from domain.workforce.handoff import Delivery, compatible, delivered
from domain.workforce.performance import AssignmentFact, Outcome, measure
from domain.workforce.protocols import Plan
from domain.workforce.readiness import (
    ModelFacts,
    ReadinessFacts,
    ReadinessState,
    ReasonCode,
    ToolFacts,
    assess,
)
from domain.workforce.readiness import (
    Recovery as Fix,
)
from domain.workforce.routing import Requirement
from domain.workspace.models import WorkspaceId
from infrastructure.employees.yaml_registry import YamlEmployeeRegistry
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.workforce import FakeRegistry, RecordingExecution

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

READ = ToolFacts(effect=Effect.READ, capabilities=frozenset({Capability.FILE_ACCESS}))
WRITE = ToolFacts(effect=Effect.WRITE, capabilities=frozenset({Capability.FILE_ACCESS}))
RUN = ToolFacts(effect=Effect.EXECUTE, capabilities=frozenset({Capability.CODE}))


def facts(tools=None, integrations=None, model=True, declared=None) -> ReadinessFacts:
    return ReadinessFacts(
        tools=tools if tools is not None else {"fs.read": READ, "fs.write": WRITE, "code.run": RUN},
        integrations=integrations or {},
        model=ModelFacts(available=model, detail="" if model else "no model fits"),
        declared_integrations=declared,
    )


def with_observations(task: Task, *observations: dict) -> Task:
    return replace(
        task,
        status=TaskStatus.COMPLETED,
        result=TaskResult(summary="done", output={"observations": list(observations)}),
    )


def wrote(path: str) -> dict:
    return {
        "step": 1,
        "summary": "wrote",
        "succeeded": True,
        "details": {"tool": "fs.write", "wrote": path},
    }


# --- The contract, as a declaration -------------------------------------------


def _declare(tmp_path: Path, body: str) -> YamlEmployeeRegistry:
    folder = tmp_path / "scribe"
    folder.mkdir()
    (folder / "employee.yaml").write_text("name: scribe\nrole: Scribe\n" + body, encoding="utf-8")
    return YamlEmployeeRegistry(tmp_path)


def test_a_declaration_without_a_contract_loads_with_defaults_that_say_so(tmp_path: Path) -> None:
    loaded = _declare(tmp_path, "").get("scribe")

    assert loaded.contract == UNDECLARED
    assert loaded.contract.declared is False
    assert loaded.contract.accepts == frozenset(), "accepts anything, as before"
    assert loaded.contract.produces == {WorkProduct.ANSWER}
    assert loaded.contract.failure_kinds == frozenset(FailureKind)


def test_a_declared_contract_is_read_strictly(tmp_path: Path) -> None:
    loaded = _declare(
        tmp_path, "contract:\n  accepts: [findings]\n  produces: [FILE]\n  evidence: [ARTIFACT]\n"
    ).get("scribe")

    assert loaded.contract.declared
    assert loaded.contract.accepts == {WorkProduct.FINDINGS}
    assert loaded.contract.produces == {WorkProduct.FILE, WorkProduct.ANSWER}
    assert loaded.contract.evidence == {EvidenceKind.ARTIFACT}


def test_a_misspelt_contract_field_is_refused_with_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="produce"):
        _declare(tmp_path, "contract:\n  produce: [FILE]\n").get("scribe")


def test_an_unknown_product_is_refused_with_the_vocabulary(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Known: ANSWER"):
        _declare(tmp_path, "contract:\n  produces: [POEM]\n").get("scribe")


def test_every_shipped_employee_declares_a_contract() -> None:
    registry = YamlEmployeeRegistry()
    assert all(item.contract.declared for item in registry.list())


# --- Readiness ----------------------------------------------------------------


def test_a_role_with_everything_it_declares_is_ready() -> None:
    reader = definition(
        "reader", tools=frozenset({"fs.read"}), capabilities=frozenset({Capability.FILE_ACCESS})
    )

    verdict = assess(reader, facts())

    assert verdict.state is ReadinessState.READY
    assert verdict.reasons == ()


def test_a_required_integration_that_is_not_connected_makes_the_role_unavailable() -> None:
    mailer = definition("mailer", integrations=frozenset({"gmail"}))

    verdict = assess(mailer, facts(declared=frozenset({"gmail"})))

    assert verdict.state is ReadinessState.UNAVAILABLE
    reason = verdict.reasons[0]
    assert reason.code is ReasonCode.INTEGRATION_NOT_CONNECTED
    assert reason.recovery is Fix.PLUGINS
    assert "gmail" in reason.recovery_hint


def test_a_window_grant_that_is_disconnected_only_degrades() -> None:
    helper = definition("helper", integrations=frozenset({"notes"}))

    verdict = assess(helper, facts(integrations={"notes": False}, declared=frozenset()))

    assert verdict.state is ReadinessState.DEGRADED
    assert verdict.reasons[0].code is ReasonCode.GRANTED_INTEGRATION_UNAVAILABLE


def test_losing_the_tool_behind_every_claimed_capability_is_unavailable() -> None:
    coder = definition(
        "coder", tools=frozenset({"code.run"}), capabilities=frozenset({Capability.CODE})
    )

    verdict = assess(coder, facts(tools={"fs.read": READ}))

    assert verdict.state is ReadinessState.UNAVAILABLE
    assert {reason.code for reason in verdict.reasons} >= {
        ReasonCode.REQUIRED_TOOL_MISSING,
        ReasonCode.CAPABILITY_UNBACKED,
    }
    assert verdict.lost_capabilities == {Capability.CODE}


def test_losing_one_of_several_capabilities_degrades_and_names_what_was_lost() -> None:
    analyst = definition(
        "analyst",
        tools=frozenset({"fs.read", "code.run"}),
        capabilities=frozenset({Capability.CODE, Capability.FILE_ACCESS}),
    )

    verdict = assess(analyst, facts(tools={"fs.read": READ}))

    assert verdict.state is ReadinessState.DEGRADED
    assert verdict.lost_capabilities == {Capability.CODE}
    assert verdict.assignable


def test_no_model_able_to_run_it_is_unavailable_and_an_unanswerable_check_is_never_ready() -> None:
    reader = definition("reader", tools=frozenset({"fs.read"}))

    assert assess(reader, facts(model=False)).state is ReadinessState.UNAVAILABLE
    unknown = assess(reader, facts(model=None))
    assert unknown.state is ReadinessState.DEGRADED
    assert unknown.reasons[0].code is ReasonCode.MODEL_CHECK_UNAVAILABLE


def test_a_policy_that_denies_the_declared_deliverable_is_unavailable() -> None:
    blocked = replace(
        definition(
            "blocked", tools=frozenset({"fs.read", "fs.write"}), policies=frozenset({"read_only"})
        ),
        contract=WorkContract(produces=frozenset({WorkProduct.FILE}), declared=True),
    )

    verdict = assess(blocked, facts())

    assert verdict.state is ReadinessState.UNAVAILABLE
    codes = {reason.code for reason in verdict.reasons}
    assert ReasonCode.POLICY_BLOCKS_DELIVERABLE in codes
    assert ReasonCode.POLICY_DENIES_TOOL in codes


def test_connecting_a_dependency_changes_readiness_without_a_restart() -> None:
    mailer = definition("mailer", integrations=frozenset({"gmail"}))
    connected: list[Integration] = []
    service = ReadinessService(
        tools=lambda: None,
        integrations=lambda: tuple(connected),
        declared_integrations=lambda name: frozenset({"gmail"}),
    )

    assert service.readiness(mailer).state is ReadinessState.UNAVAILABLE

    connected.append(Integration(id=uuid4(), name="gmail", status=IntegrationStatus.CONNECTED))

    assert service.readiness(mailer).state is ReadinessState.READY


def test_the_model_check_asks_the_router_for_every_stage_of_a_run() -> None:
    class Router:
        def select(self, kind, requirement, hints):
            if requirement.min_context_tokens:
                raise ConfigurationError("No model in the catalog can do this work")

    from domain.llm.models import RoutingHints, TaskKind

    reader = definition("reader")
    service = ReadinessService(
        tools=lambda: None,
        router=lambda: Router(),
        stages=lambda _: (
            (TaskKind.PLANNING, CapabilityRequirement(), RoutingHints()),
            (TaskKind.EXECUTION, CapabilityRequirement(min_context_tokens=10**9), RoutingHints()),
        ),
    )

    verdict = service.readiness(reader)

    assert verdict.state is ReadinessState.UNAVAILABLE
    assert verdict.reasons[0].code is ReasonCode.NO_SUITABLE_MODEL
    assert "Execution" in verdict.reasons[0].message


# --- Acceptance against the contract ------------------------------------------


def test_a_role_that_promises_a_file_is_not_accepted_without_one() -> None:
    contract = WorkContract(
        produces=frozenset({WorkProduct.FILE}),
        evidence=frozenset({EvidenceKind.ARTIFACT}),
        declared=True,
    )
    ran = {"step": 1, "summary": "read", "succeeded": True, "details": {"tool": "fs.read"}}

    refused = accept(with_observations(Task.create("Write it"), ran), contract)
    taken = accept(with_observations(Task.create("Write it"), ran, wrote("out.md")), contract)

    assert not refused.accepted and refused.code is AcceptanceCode.EVIDENCE_MISSING
    assert taken.accepted


def test_without_a_contract_judgement_with_no_tools_is_still_accepted() -> None:
    assert accept(with_observations(Task.create("Decide"))).accepted
    promised = WorkContract(evidence=frozenset({EvidenceKind.TOOL_RESULT}), declared=True)
    assert not accept(with_observations(Task.create("Decide")), promised).accepted


def test_stored_verdicts_and_decisions_from_an_unknown_format_read_as_unrecorded() -> None:
    assert Decision.from_dict({}) is None
    assert Decision.from_dict({"version": 99, "code": "MODEL_CHOSE"}) is None
    assert Decision.from_dict({"version": 1, "code": "SOMETHING_NEW"}) is None
    assert Acceptance.from_dict(None) is None
    assert Acceptance.from_dict({"accepted": "yes"}) is None
    decision = Decision(
        SelectionCode.ONLY_QUALIFIED,
        "the only one",
        alternatives=(),
    )
    assert Decision.from_dict(json.loads(json.dumps(decision.to_dict()))) == decision


# --- Hand-offs ----------------------------------------------------------------


def test_what_was_delivered_is_read_off_the_record() -> None:
    contract = WorkContract(produces=frozenset({WorkProduct.FILE}), declared=True)

    promised_only = delivered(with_observations(Task.create("x")), "writer", contract)
    kept = delivered(with_observations(Task.create("x"), wrote("a.md")), "writer", contract)

    assert WorkProduct.FILE not in promised_only.products
    assert WorkProduct.FILE in kept.products


def test_a_role_that_accepts_findings_cannot_start_from_changes_alone() -> None:
    writer = WorkContract(accepts=frozenset({WorkProduct.FINDINGS}), declared=True)
    moved = Delivery("organizer", frozenset({WorkProduct.CHANGES, WorkProduct.ANSWER}))

    assert not compatible(writer, (moved,))
    assert compatible(writer, ())
    assert compatible(UNDECLARED, (moved,))


# --- Delegation with readiness and hand-offs ----------------------------------


class Readiness:
    """A readiness answer per name, defaulting to READY."""

    def __init__(self, **states: ReadinessFacts) -> None:
        self._facts = states

    def readiness(self, definition):
        return assess(definition, self._facts.get(definition.name, facts()))


READER = definition("reader", tools=frozenset({"fs.read"}))
MAILER = definition("mailer", integrations=frozenset({"gmail"}))


async def test_an_unavailable_employee_is_never_given_work_and_the_record_says_why() -> None:
    llm = FakeLLM()  # a model call would mean somebody unavailable was still a candidate
    delegator = CapabilityDelegator(
        llm,
        FakeRegistry(READER, MAILER),
        readiness=Readiness(mailer=facts(declared=frozenset({"gmail"}))),
    )

    delegation = await delegator.decide(Task.create("Read the notes"))

    assert delegation.chosen is READER
    assert llm.call_count == 0
    assert delegation.decision.code is SelectionCode.ONLY_QUALIFIED
    [passed_over] = delegation.decision.alternatives
    assert passed_over.employee == "mailer"
    assert passed_over.code is RejectionCode.UNAVAILABLE
    assert "gmail" in passed_over.reason


async def test_a_workforce_nobody_in_which_can_work_is_a_delegation_error() -> None:
    delegator = CapabilityDelegator(
        FakeLLM(),
        FakeRegistry(MAILER),
        readiness=Readiness(mailer=facts(declared=frozenset({"gmail"}))),
    )

    with pytest.raises(DelegationError, match="gmail"):
        await delegator.choose(Task.create("Send it"))


class DeclaredRegistry(FakeRegistry):
    """Searches the declared capabilities, as the real registries do."""

    def find_by_capability(self, requirement):
        return [d for d in self.list() if requirement.is_satisfied_by(d.capabilities)]


async def test_a_degraded_role_is_kept_away_from_exactly_the_work_it_lost() -> None:
    coder = definition(
        "coder",
        tools=frozenset({"fs.read", "code.run"}),
        capabilities=frozenset({Capability.CODE, Capability.FILE_ACCESS}),
    )
    other = definition(
        "other", tools=frozenset({"code.run"}), capabilities=frozenset({Capability.CODE})
    )
    delegator = CapabilityDelegator(
        FakeLLM(),
        DeclaredRegistry(coder, other),
        readiness=Readiness(coder=facts(tools={"fs.read": READ})),
    )

    delegation = await delegator.decide(
        Task.create("Compute it"),
        requirement=Requirement(CapabilityRequirement(required=frozenset({Capability.CODE}))),
    )

    assert delegation.chosen is other
    assert delegation.decision.alternatives[0].code is RejectionCode.CAPABILITY_LOST_HERE


async def test_two_roles_nothing_tells_apart_are_recorded_as_indistinguishable() -> None:
    twin = definition("twin", tools=frozenset({"fs.read"}))
    llm = FakeLLM([reply(json.dumps({"employee": "twin", "reason": "either"}))])
    delegator = CapabilityDelegator(llm, FakeRegistry(READER, twin))

    delegation = await delegator.decide(Task.create("Read it"))

    assert delegation.decision.code is SelectionCode.MODEL_CHOSE_AMONG_EQUALS
    assert delegation.decision.indistinguishable == ("reader", "twin")
    assert delegation.decision.alternatives[0].code is RejectionCode.NOT_CHOSEN


async def test_a_hand_off_nobody_can_start_from_chooses_nobody() -> None:
    writer = replace(
        definition("writer", tools=frozenset({"fs.write"})),
        contract=WorkContract(accepts=frozenset({WorkProduct.FINDINGS}), declared=True),
    )
    delegator = CapabilityDelegator(FakeLLM(), FakeRegistry(writer))

    delegation = await delegator.decide(
        Task.create("Write it up"),
        upstream=(Delivery("mover", frozenset({WorkProduct.CHANGES, WorkProduct.ANSWER})),),
    )

    assert delegation.chosen is None
    assert delegation.decision.code is SelectionCode.NO_COMPATIBLE_EMPLOYEE
    assert delegation.decision.alternatives[0].code is RejectionCode.HANDOFF_INCOMPATIBLE
    assert "mover" in delegation.reason


async def test_an_incompatible_hand_off_is_refused_before_the_downstream_task_starts() -> None:
    # Neither can start from a folder that was merely rearranged.
    mover = replace(
        definition("mover", tools=frozenset({"fs.move"})),
        contract=WorkContract(
            accepts=frozenset({WorkProduct.FILE}),
            produces=frozenset({WorkProduct.CHANGES}),
            declared=True,
        ),
    )
    writer = replace(
        definition("writer", tools=frozenset({"fs.write"})),
        contract=WorkContract(accepts=frozenset({WorkProduct.FINDINGS}), declared=True),
    )
    execution = RecordingExecution()
    chooser = FakeLLM([reply(json.dumps({"employee": "mover", "reason": "it moves files"}))])
    supervisor = Supervisor(
        execution=execution, delegator=CapabilityDelegator(chooser, FakeRegistry(mover, writer))
    )
    tasks = (Task.create("Sort the folder"), Task.create("Write a summary of it"))
    plan = Plan.create(tasks[0].id, tasks=tasks, dependencies=((tasks[1].id, tasks[0].id),))

    result = await supervisor.run(plan)

    assert execution.goals == ["Sort the folder"], "the write-up was never started"
    assert not result.all_succeeded
    assert result.recovery is Recovery.REPLAN
    assert "Nobody can start from what the earlier work delivered" in result.shortfall[0]
    assert result.outcomes[-1].never_started


async def test_the_decision_is_carried_on_the_assignment_the_task_is_started_with() -> None:
    execution = RecordingExecution()
    supervisor = Supervisor(
        execution=execution, delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(READER))
    )

    await supervisor.run(Plan.create(uuid4(), tasks=(Task.create("Read it"),)))

    [(_, assignment)] = execution.started
    assert assignment.decision is not None
    assert assignment.decision.code is SelectionCode.ONLY_EMPLOYEE


async def test_a_task_given_to_somebody_before_a_crash_is_resumed_not_delegated_again() -> None:
    execution = RecordingExecution()
    chooser = FakeLLM()  # delegating again would ask it and run out of script
    supervisor = Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(chooser, FakeRegistry(READER, definition("other"))),
    )
    written = replace(Task.create("Read it"), assigned_employee_id=READER.id)

    result = await supervisor.run(Plan.create(uuid4(), tasks=(written,)))

    assert result.all_succeeded
    assert chooser.call_count == 0
    [(resumed, _)] = execution.started
    assert resumed.id == written.id


# --- Statistics ---------------------------------------------------------------


def _fact(
    status: TaskStatus,
    *,
    cost=0.1,
    minutes=5,
    acceptance=None,
    error=None,
    approvals=0,
    output=None,
):
    task = replace(
        Task.create("work"),
        status=status,
        cost_usd=cost,
        result=TaskResult(summary="", output=output or {}),
        error=error,
    )
    assigned = NOW - timedelta(days=1)
    return AssignmentFact(
        task=task,
        assigned_at=assigned,
        closed_at=assigned + timedelta(minutes=minutes) if task.is_terminal else None,
        acceptance=acceptance,
        approvals=approvals,
    )


def test_no_history_is_no_data_rather_than_a_rate() -> None:
    record = measure([], now=NOW)

    assert not record.has_history
    assert record.accepted_rate.value is None
    assert record.accepted_rate.note.startswith("No data")
    assert record.cost_per_accepted_usd.value is None
    assert record.scenario_pass_rate.value is None


def test_outcomes_are_kept_apart_and_cancellation_is_not_in_the_denominator() -> None:
    refused = Acceptance(
        accepted=False, reason="refused", refused=True, code=AcceptanceCode.ALL_REFUSED
    )
    not_accepted = Acceptance(
        accepted=False, reason="no file", code=AcceptanceCode.EVIDENCE_MISSING
    )
    facts_ = [
        *(_fact(TaskStatus.COMPLETED, acceptance=Acceptance.taken()) for _ in range(3)),
        _fact(TaskStatus.COMPLETED, acceptance=not_accepted),
        _fact(TaskStatus.COMPLETED, acceptance=refused),
        _fact(TaskStatus.FAILED, error=TaskError("RateLimitError", "slow down")),
        _fact(TaskStatus.CANCELLED),
        _fact(TaskStatus.CANCELLED),
        _fact(TaskStatus.RUNNING),
    ]

    record = measure(facts_, now=NOW)

    assert record.assignments == 9
    assert record.outcomes[Outcome.ACCEPTED] == 3
    assert record.outcomes[Outcome.NOT_ACCEPTED] == 1
    assert record.outcomes[Outcome.REFUSED] == 1
    assert record.outcomes[Outcome.FAILED] == 1
    assert record.outcomes[Outcome.CANCELLED] == 2
    assert record.outcomes[Outcome.OPEN] == 1
    # Six decided; cancelled and open are not the employee's result either way.
    assert record.accepted_rate.sample == 6
    assert record.accepted_rate.value == 0.5
    assert record.failures[FailureKind.TRANSIENT] == 1
    # The price of a result includes what failed on the way to it.
    assert record.cost_per_accepted_usd.value == pytest.approx(0.6 / 3)


def test_a_rate_below_the_minimum_sample_has_no_value_and_says_how_far_short() -> None:
    record = measure([_fact(TaskStatus.COMPLETED, acceptance=Acceptance.taken())], now=NOW)

    assert record.accepted_rate.value is None
    assert record.accepted_rate.note == "Not enough data: 1 of 5 decided assignments."
    assert record.cost_per_accepted_usd.value == pytest.approx(0.1)
    assert record.p95_latency_seconds.value is None


def test_a_verdict_never_stored_is_derived_from_the_record_and_counted_as_derived() -> None:
    all_failed = {"observations": [{"succeeded": False, "details": {"tool": "fs.read"}}]}

    record = measure([_fact(TaskStatus.COMPLETED, output=all_failed)], now=NOW)

    assert record.derived_verdicts == 1
    assert record.outcomes[Outcome.NOT_ACCEPTED] == 1


def test_work_outside_the_window_is_not_counted() -> None:
    old = replace(_fact(TaskStatus.COMPLETED), assigned_at=NOW - timedelta(days=40))

    assert measure([old], now=NOW).assignments == 0


def test_the_same_pattern_in_one_workspace_has_one_stable_suggestion_id() -> None:
    pattern = Pattern(
        fingerprint="same-structure",
        steps=(StepShape("reader"), StepShape("writer", depends_on=(0,))),
        sources=(uuid4(), uuid4(), uuid4()),
        first_seen=NOW - timedelta(days=3),
        last_seen=NOW,
    )

    first = reconcile(None, pattern, workspace_id=WorkspaceId("alpha"), now=NOW)
    concurrent = reconcile(None, pattern, workspace_id=WorkspaceId("alpha"), now=NOW)
    elsewhere = reconcile(None, pattern, workspace_id=WorkspaceId("beta"), now=NOW)

    assert first is not None and concurrent is not None and elsewhere is not None
    assert first.id == concurrent.id
    assert first.id != elsewhere.id


# --- What must not leak -------------------------------------------------------


async def test_a_stated_reason_that_mentions_a_credential_is_not_recorded() -> None:
    twin = definition("twin", tools=frozenset({"fs.read"}))
    llm = FakeLLM(
        [reply(json.dumps({"employee": "twin", "reason": "use api_key sk-live-123 for this"}))]
    )
    delegator = CapabilityDelegator(llm, FakeRegistry(READER, twin))

    delegation = await delegator.decide(Task.create("Read it"))

    stored = json.dumps(delegation.decision.to_dict())
    assert "sk-live-123" not in stored
    assert "withheld" in delegation.reason


def test_a_profile_carries_no_prompt_and_no_integration_configuration() -> None:
    from application.interface import views
    from application.workforce.profiles import EmployeeRecord
    from domain.workforce.profile import build

    secretive = replace(
        definition(
            "secretive", integrations=frozenset({"gmail"}), system_prompt="TOP-SECRET-PROMPT"
        ),
    )
    profile = build(secretive, facts(integrations={"gmail": True}), assess(secretive, facts()))
    rendered = json.dumps(views.employee_profile(EmployeeRecord(profile, measure([], now=NOW), ())))

    assert "TOP-SECRET-PROMPT" not in rendered
    assert "configuration" not in rendered
