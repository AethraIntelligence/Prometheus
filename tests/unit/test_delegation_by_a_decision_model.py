"""Who does the work, asked first of a model built to decide.

The contract with the rest of delegation is narrow: the decision model's
choice stands only when it names a candidate and is *measured* as sure;
anything else - doubt, silence, an error, no such model at all - is the
question asked the long way, exactly as before it existed.
"""

from __future__ import annotations

import json

from application.prometheus.delegation import DELEGATION_CONFIDENCE, CapabilityDelegator
from domain.decisions.models import ChoiceAnswer
from domain.errors import ProviderUnavailableError
from domain.tasks.task import Task
from domain.workforce.decision import SelectionCode
from infrastructure.decisions.routed import RoutedDecider
from infrastructure.llm.catalog import ModelCatalog
from tests.fakes.decisions import FakeDecider
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.workforce import FakeRegistry

READER = definition("reader", tools=frozenset({"fs.read"}))
WRITER = definition("writer", tools=frozenset({"fs.write"}))
TASK = Task.create("Write the summary to notes/summary.md")


def by_text(name: str = "reader") -> FakeLLM:
    return FakeLLM([reply(json.dumps({"employee": name, "reason": "the long way"}))])


async def test_a_sure_decision_model_decides_and_the_text_model_is_not_asked() -> None:
    llm = by_text()
    decider = FakeDecider([ChoiceAnswer(key="writer", confidence=0.91, source="decision model")])

    delegation = await CapabilityDelegator(
        llm, FakeRegistry(READER, WRITER), decider=decider
    ).decide(TASK)

    assert delegation.chosen is WRITER
    assert delegation.decision.code is SelectionCode.MODEL_CHOSE
    assert "91% sure" in delegation.reason
    assert llm.call_count == 0
    [question] = decider.questions
    assert question.state == TASK.goal
    assert question.keys == ("reader", "writer")
    assert "fs.write" in question.options[1].text


async def test_doubt_sends_the_question_the_long_way() -> None:
    llm = by_text("reader")
    doubtful = ChoiceAnswer(key="writer", confidence=DELEGATION_CONFIDENCE - 0.01)

    delegation = await CapabilityDelegator(
        llm, FakeRegistry(READER, WRITER), decider=FakeDecider([doubtful])
    ).decide(TASK)

    assert delegation.chosen is READER
    assert delegation.reason == "the long way"
    assert llm.call_count == 1


async def test_an_unmeasured_choice_is_not_taken_on_trust() -> None:
    llm = by_text("reader")

    delegation = await CapabilityDelegator(
        llm, FakeRegistry(READER, WRITER), decider=FakeDecider([ChoiceAnswer(key="writer")])
    ).decide(TASK)

    assert delegation.chosen is READER and llm.call_count == 1


async def test_a_decision_model_that_fails_does_not_fail_the_hand_off() -> None:
    llm = by_text("reader")
    broken = FakeDecider([ProviderUnavailableError("overloaded")])

    delegation = await CapabilityDelegator(
        llm, FakeRegistry(READER, WRITER), decider=broken
    ).decide(TASK)

    assert delegation.chosen is READER


async def test_one_candidate_asks_nobody() -> None:
    decider = FakeDecider()

    await CapabilityDelegator(FakeLLM(), FakeRegistry(WRITER), decider=decider).decide(TASK)

    assert decider.questions == []


def routes(decision: str, *, jev_quality: float = 0.6) -> ModelCatalog:
    return ModelCatalog.from_dict(
        {
            "models": {
                "writer": {"provider": "local", "model": "small", "capabilities": ["CODE"]},
                "jev": {
                    "provider": "typesafe",
                    "model": "jev-latest",
                    "capabilities": ["DECISION"],
                    "quality": jev_quality,
                },
            },
            "defaults": {"decision": decision},
        }
    )


async def test_with_no_decision_model_routed_nothing_changes() -> None:
    """The composition root's delegation decider has no text model behind it."""
    llm, typed = by_text("reader"), FakeDecider()
    decider = RoutedDecider(None, catalog=lambda: routes("writer"), typed=lambda _: typed)

    delegation = await CapabilityDelegator(
        llm, FakeRegistry(READER, WRITER), decider=decider
    ).decide(TASK)

    assert delegation.chosen is READER
    assert typed.questions == [] and llm.call_count == 1


async def test_a_decision_model_below_the_floor_is_not_asked() -> None:
    typed = FakeDecider([ChoiceAnswer(key="writer", confidence=0.99)])
    decider = RoutedDecider(
        None,
        catalog=lambda: routes("jev", jev_quality=0.3),
        typed=lambda _: typed,
        min_quality=0.5,
    )

    delegation = await CapabilityDelegator(
        by_text("reader"), FakeRegistry(READER, WRITER), decider=decider
    ).decide(TASK)

    assert delegation.chosen is READER
    assert typed.questions == []
