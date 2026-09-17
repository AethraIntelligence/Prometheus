"""Phase 10: a model has a contract, and a failure it caused buys a better model.

The contract is what routing may rely on: capabilities, context, quality tier,
privacy, latency, cost. Escalation is the adaptive half: routing stays cheap by
default and steps up one tier after a failure a stronger model plausibly fixes
- and only then, and says so in the reason every call records.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from application.prometheus.delegation import CapabilityDelegator
from application.prometheus.supervisor import Supervisor
from domain.capabilities.models import CapabilityRequirement
from domain.errors import ConfigurationError
from domain.llm import escalation, routing
from domain.llm.catalog import ModelEntry, Privacy
from domain.llm.escalation import Escalation, EscalationCause, cause_of
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.telemetry import LLMCallRecord
from domain.tasks.task import Task, TaskError, TaskResult, TaskStatus
from domain.workforce import directions
from domain.workforce.protocols import Plan
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.directed import DirectedLLM
from infrastructure.llm.router import CapabilityAwareModelRouter
from infrastructure.llm.telemetry import MeteredLLM
from infrastructure.persistence.llm_call_repository import InMemoryLLMCallLog
from infrastructure.progress.broadcaster import InMemoryProgressBroadcaster
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.workforce import FakeRegistry, RecordingExecution, refused_a_tool

REPO_ROOT = Path(__file__).resolve().parents[2]

CATALOG = {
    "models": {
        "fast": {"provider": "local", "model": "small", "quality": 0.4,
                 "capabilities": ["TEXT_REASONING"]},
        "fast-remote": {"provider": "openrouter", "model": "tiny", "quality": 0.45,
                        "capabilities": ["TEXT_REASONING"], "input_cost_per_1k_usd": 0.0001},
        "balanced": {"provider": "openrouter", "model": "medium", "quality": 0.6,
                     "capabilities": ["TEXT_REASONING"], "input_cost_per_1k_usd": 0.003},
        "strong": {"provider": "openrouter", "model": "large", "quality": 0.9,
                   "capabilities": ["TEXT_REASONING"], "input_cost_per_1k_usd": 0.015},
        "strong-cheap": {"provider": "openrouter", "model": "large-lite", "quality": 0.8,
                         "capabilities": ["TEXT_REASONING"], "input_cost_per_1k_usd": 0.005},
    },
    "defaults": {"execution": "fast", "planning": "fast-remote"},
}  # fmt: skip


def router(**kwargs) -> CapabilityAwareModelRouter:
    return CapabilityAwareModelRouter(ModelCatalog.from_dict(CATALOG), **kwargs)


def select(r: CapabilityAwareModelRouter, kind: TaskKind = TaskKind.EXECUTION, **req):
    return r.select(kind, CapabilityRequirement(**req), RoutingHints())


# --- The contract --------------------------------------------------------------


def test_a_contract_states_tier_privacy_latency_and_cost() -> None:
    catalog = ModelCatalog.from_dict(
        {"models": {"m": {"provider": "openrouter", "model": "x", "quality": 0.8,
                          "latency_ms": 900, "input_cost_per_1k_usd": 0.004,
                          "output_cost_per_1k_usd": 0.008}}}
    )  # fmt: skip
    contract = catalog.get("m").contract

    assert contract.tier == "STRONG"
    assert contract.privacy is Privacy.REMOTE
    assert contract.latency_ms == 900
    assert contract.estimated_cost_per_1k_usd == pytest.approx(0.006)


@pytest.mark.parametrize(
    "field, value",
    [
        ("context_tokens", 0),
        ("quality", 1.1),
        ("input_cost_per_1k_usd", -0.01),
        ("output_cost_per_1k_usd", -0.01),
        ("dimensions", -1),
        ("latency_ms", -1),
        ("quality", float("nan")),
        ("input_cost_per_1k_usd", float("inf")),
    ],
)
def test_contract_numbers_that_would_invert_routing_are_refused(field, value) -> None:
    with pytest.raises(ValueError):
        ModelEntry(name="broken", provider="local", model="m", **{field: value})


def test_the_local_catalog_says_which_local_entry_leaves_the_machine() -> None:
    catalog = ModelCatalog.load(REPO_ROOT / "infrastructure/llm/models.local.toml")

    assert catalog.get("local-strong").privacy is Privacy.REMOTE, "a cloud-served model"
    assert catalog.get("local-fast").privacy is Privacy.LOCAL


def test_local_only_filters_remote_models_and_says_why_the_default_lost() -> None:
    choice = select(router(), TaskKind.PLANNING, local_only=True)

    assert choice.entry == "fast"
    assert choice.privacy == "LOCAL"
    assert "default 'fast-remote' cannot do this work" in choice.reason
    with pytest.raises(ConfigurationError, match="served on this machine"):
        select(router(local_only=True), min_quality=0.5)


def test_latency_ranks_models_nobody_made_the_default() -> None:
    catalog = ModelCatalog.from_dict(
        {"models": {
            "slow": {"provider": "openrouter", "model": "a", "quality": 0.7, "latency_ms": 20000},
            "quick": {"provider": "openrouter", "model": "b", "quality": 0.7, "latency_ms": 500},
        }}
    )  # fmt: skip
    choice = CapabilityAwareModelRouter(catalog).select(
        TaskKind.SYNTHESIS, CapabilityRequirement(), RoutingHints(latency_sensitivity=1.0)
    )

    assert choice.entry == "quick"


# --- Escalating ----------------------------------------------------------------


def test_escalation_moves_to_the_cheapest_model_of_the_next_tier() -> None:
    with escalation.given(Escalation(1, EscalationCause.VERIFICATION_REJECTED)):
        choice = select(router())

    assert choice.entry == "balanced", "one tier up, not straight to the top"
    assert choice.escalation_level == 1
    assert "escalated from 'fast' (fast) to balanced after verification rejected" in choice.reason


def test_escalating_from_the_top_keeps_the_model_and_says_there_is_nothing_above() -> None:
    top = {**CATALOG, "defaults": {"execution": "strong"}}
    r = CapabilityAwareModelRouter(ModelCatalog.from_dict(top))

    with escalation.given(Escalation(1, EscalationCause.STEP_BUDGET)):
        choice = select(r)

    assert choice.entry == "strong"
    assert choice.escalation_level == 0
    assert "no stronger model to escalate to after step budget" in choice.reason
    assert not r.stronger_available(TaskKind.EXECUTION)
    assert router().stronger_available(TaskKind.EXECUTION)


def test_the_persons_chosen_model_is_not_escalated_past() -> None:
    with (
        directions.given(directions.Directions(model="fast")),
        escalation.given(Escalation(1, EscalationCause.PLANNING_FAILED)),
    ):
        choice = select(router())

    assert (choice.entry, choice.reason) == ("fast", "chosen for this request")
    with directions.given(directions.Directions(model="fast")):
        assert not router().stronger_available(TaskKind.EXECUTION)


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"completed": False, "error_kind": "VerificationFailed"},
         EscalationCause.VERIFICATION_REJECTED),
        ({"completed": False, "error_kind": "PlanningError"}, EscalationCause.PLANNING_FAILED),
        ({"completed": False, "stopped_by": "STEPS"}, EscalationCause.STEP_BUDGET),
        ({"completed": True, "accepted": False}, EscalationCause.NOT_ACCEPTED),
        ({"completed": False, "stopped_by": "COST", "error_kind": "VerificationFailed"}, None),
        ({"completed": False, "error_kind": "RateLimitError"}, None),
        ({"completed": False, "error_kind": "VerificationFailed", "refused": True}, None),
        ({"completed": True, "accepted": True}, None),
    ],
)  # fmt: skip
def test_only_failures_a_stronger_model_can_fix_escalate(record, expected) -> None:
    assert cause_of(**record) is expected


def test_an_escalation_survives_being_stored_on_an_assignment() -> None:
    stored = Escalation(1, EscalationCause.NOT_ACCEPTED).to_dict()

    assert Escalation.from_dict(stored) == Escalation(1, EscalationCause.NOT_ACCEPTED)
    assert Escalation.from_dict({"level": 7}).level == 1, "capped at the maximum"
    assert not Escalation.from_dict("nonsense").active


# --- The trace -----------------------------------------------------------------


async def test_every_call_records_the_entry_the_reason_and_the_task() -> None:
    r = router()
    log = InMemoryLLMCallLog()
    catalog = ModelCatalog.from_dict(CATALOG)
    clients = {}

    def route():
        choice = r.select(TaskKind.EXECUTION, CapabilityRequirement(), RoutingHints())
        client = clients.setdefault(
            choice.entry,
            MeteredLLM(FakeLLM([reply("ok", model=choice.model)] * 3),
                       provider=choice.provider, catalog=catalog, call_log=log),
        )  # fmt: skip
        return client, choice

    llm = DirectedLLM(route(), route, task_kind=TaskKind.EXECUTION)
    task = Task.create("Do it")
    request = LLMRequest(messages=(Message.user("hi"),))

    with routing.billing(task.id):
        await llm.generate(request)
        with escalation.given(Escalation(1, EscalationCause.VERIFICATION_REJECTED)):
            await llm.generate(request)

    first, second = log.calls
    assert isinstance(first, LLMCallRecord)
    assert (first.task_id, first.entry, first.task_kind) == (task.id, "fast", "EXECUTION")
    assert first.reason == "configured default for execution"
    assert (second.entry, second.escalation_level) == ("balanced", 1)
    assert "escalated from 'fast'" in second.reason


# --- The supervisor ------------------------------------------------------------


class Advisor:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.asked: list[TaskKind] = []

    def stronger_available(self, task_kind, required=None) -> bool:
        self.asked.append(task_kind)
        return self.available


def rejected(task: Task, assignment) -> Task:
    if assignment.context.data.get("escalation"):
        return replace(task, status=TaskStatus.COMPLETED, result=TaskResult(summary="done well"))
    return replace(
        task,
        status=TaskStatus.FAILED,
        error=TaskError(kind="VerificationFailed", message="thin"),
        result=TaskResult(summary="thin"),
    )


def supervisor(execution, *, advisor=None, progress=None) -> Supervisor:
    return Supervisor(
        execution=execution,
        delegator=CapabilityDelegator(FakeLLM(), FakeRegistry(definition("reader"))),
        progress=progress,
        escalation=advisor,
    )


def one_task() -> Plan:
    task = Task.create("Write the summary")
    return Plan.create(task.id, tasks=(task,))


async def test_a_rejected_task_is_tried_once_more_on_a_stronger_model() -> None:
    execution = RecordingExecution(rejected)
    progress = InMemoryProgressBroadcaster()
    plan = one_task()
    advisor = Advisor()

    result = await supervisor(execution, advisor=advisor, progress=progress).run(plan)

    assert result.all_succeeded
    contexts = [assignment.context.data.get("escalation") for _, assignment in execution.started]
    assert contexts[:-1] == [None] * (len(contexts) - 1)
    assert contexts[-1] == {"level": 1, "cause": "VERIFICATION_REJECTED"}
    announced = [
        event.payload.get("escalation")
        for event in progress.recent(plan.tasks[0].id)
        if event.payload.get("escalation")
    ]
    assert announced == [{"level": 1, "cause": "VERIFICATION_REJECTED"}], "said in the trace"
    assert advisor.asked == [TaskKind.VERIFICATION]


async def test_no_escalation_without_a_stronger_model_or_for_a_refusal() -> None:
    nowhere = RecordingExecution(rejected)
    await supervisor(nowhere, advisor=Advisor(available=False)).run(one_task())
    assert all(not a.context.data.get("escalation") for _, a in nowhere.started)

    refused = RecordingExecution(refused_a_tool())
    await supervisor(refused, advisor=Advisor()).run(one_task())
    assert all(not a.context.data.get("escalation") for _, a in refused.started)

    unconfigured = RecordingExecution(rejected)
    await supervisor(unconfigured).run(one_task())
    assert all(not a.context.data.get("escalation") for _, a in unconfigured.started)


async def test_a_machine_with_no_usable_model_still_assembles_and_says_so_when_work_arrives(
    tmp_path,
) -> None:
    """Phase 13: a fresh install with no provider must start, or onboarding never opens."""
    import pytest

    from app.config.container import build_container
    from app.config.settings import Settings
    from domain.errors import ConfigurationError
    from domain.llm.models import LLMRequest, Message

    catalog = tmp_path / "models.toml"
    catalog.write_text("", encoding="utf-8")
    container = build_container(
        Settings(data_dir=tmp_path, model_catalog_path=catalog, llm_api_key=None)
    )

    llm = container.llm_for(TaskKind.PLANNING)

    with pytest.raises(ConfigurationError):
        await llm.generate(LLMRequest(messages=(Message.user("hello"),)))
