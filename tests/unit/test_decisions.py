"""Typed decisions: a lettered question, a measured confidence, and where it is routed.

What is tested is mostly what a real provider does to a one-letter answer: pads
it, wraps it in markdown, reports no probabilities, refuses to be asked for
them - and what the platform does when `decision` is routed to a model that
decides rather than writes and that model cannot answer.
"""

from __future__ import annotations

import json
from math import log

import httpx
import pytest

from application.decisions.text import TextDecider
from domain.capabilities.models import Capability, CapabilityRequirement
from domain.decisions.models import ChoiceAnswer, ChoiceQuestion, Option
from domain.errors import ConfigurationError, InvalidRequestError, RateLimitError
from domain.llm.models import (
    FinishReason,
    LLMRequest,
    LLMResponse,
    Message,
    ModelChoice,
    TaskKind,
    TokenLogprob,
    Usage,
)
from infrastructure.decisions.routed import RoutedDecider
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.factory import ProviderFactory
from infrastructure.llm.openrouter import OpenRouterProvider
from infrastructure.llm.profiles import current_profile
from infrastructure.llm.retry import RetryPolicy
from infrastructure.llm.router import CapabilityAwareModelRouter
from tests.fakes.decisions import FakeDecider
from tests.fakes.llm import FakeLLM, reply

QUESTION = ChoiceQuestion(
    state="What is the weather in Milan now?",
    question="Reply from what you know, or go and find out?",
    options=(Option("reply", "Reply."), Option("work", "Find out.")),
    purpose="test",
)


def measured(text: str, alternatives: dict[str, float]) -> LLMResponse:
    """A reply whose first token came with these probabilities."""
    return LLMResponse(
        content=text,
        model="fake/model",
        usage=Usage(),
        logprobs=(
            TokenLogprob(
                token=text,
                logprob=log(alternatives.get(text, 0.01)),
                alternatives=tuple((token, log(p)) for token, p in alternatives.items()),
            ),
        ),
    )


# --- The question ---------------------------------------------------------------


def test_a_question_needs_two_distinct_options() -> None:
    with pytest.raises(ValueError):
        ChoiceQuestion(state="s", question="q", options=(Option("a", "A"),))
    with pytest.raises(ValueError):
        ChoiceQuestion(state="s", question="q", options=(Option("a", "A"), Option("a", "B")))


def test_an_unmeasured_answer_is_taken_and_a_weak_one_is_not() -> None:
    assert ChoiceAnswer(key="work").is_sure("work", at_least=0.9)
    assert not ChoiceAnswer(key="work", confidence=0.6).is_sure("work", at_least=0.7)
    assert not ChoiceAnswer(key="reply", confidence=1.0).is_sure("work", at_least=0.7)
    assert not ChoiceAnswer.unreadable().is_sure("work", at_least=0.0)


# --- A text model answering ---------------------------------------------------------


async def test_the_options_are_lettered_and_the_letter_is_read_back() -> None:
    llm = FakeLLM([reply("B")])

    answer = await TextDecider(llm).choose(QUESTION)

    assert answer.key == "work"
    assert answer.confidence is None, "nothing was measured"
    prompt = llm.last_request.messages[-1].content
    assert "A - Reply." in prompt and "B - Find out." in prompt
    assert QUESTION.state in prompt
    assert llm.last_request.max_tokens == 5
    assert llm.last_request.top_logprobs


@pytest.mark.parametrize("text", ["**B**", " (b)", "B - find out", "\nB."])
async def test_packaging_around_the_letter_is_ignored(text: str) -> None:
    answer = await TextDecider(FakeLLM([reply(text)])).choose(QUESTION)
    assert answer.key == "work"


@pytest.mark.parametrize("text", ["", "Z", "I think we should look it up"])
async def test_an_answer_that_names_no_option_is_unreadable(text: str) -> None:
    answer = await TextDecider(FakeLLM([reply(text)])).choose(QUESTION)
    assert not answer.answered


async def test_the_letters_probabilities_are_the_confidence() -> None:
    llm = FakeLLM([measured("A", {"A": 0.6, "B": 0.2, "The": 0.2})])

    answer = await TextDecider(llm).choose(QUESTION)

    assert answer.key == "reply"
    # Renormalised over the letters: "The" is not an option.
    assert answer.confidence == pytest.approx(0.75)
    assert answer.probabilities == pytest.approx({"reply": 0.75, "work": 0.25})


async def test_the_sampled_token_alone_is_not_a_measurement() -> None:
    """Renormalised over the letters, one token is always 1.0 - which nobody measured."""
    response = LLMResponse(
        content="A", model="m", logprobs=(TokenLogprob(token="A", logprob=log(0.4)),)
    )
    answer = await TextDecider(FakeLLM([response])).choose(QUESTION)
    assert answer.key == "reply" and answer.confidence is None


async def test_a_model_that_thinks_first_is_given_room_and_remembered() -> None:
    """Found on every local model in the catalog: five tokens of `<think>` and no
    letter, which the triage read as "work" - so "Hello" was planned."""
    cut_off = LLMResponse(content="<think>\nThe user", model="m", finish_reason=FinishReason.LENGTH)
    thought = "<think>\nIt asks about today, so it is a lookup.\n</think>\n\nB"
    llm = FakeLLM([cut_off, reply(thought), reply("<think>greeting</think>A")])
    decider = TextDecider(llm)

    first = await decider.choose(QUESTION)
    second = await decider.choose(QUESTION)

    assert (first.key, second.key) == ("work", "reply")
    assert [request.max_tokens for request in llm.requests] == [5, 2048, 2048]


async def test_a_letter_inside_the_reasoning_is_not_the_answer() -> None:
    answer = await TextDecider(FakeLLM([reply("<think>A or B? A seems wrong.</think>B")])).choose(
        QUESTION
    )
    assert answer.key == "work"


async def test_an_unfinished_thought_is_unreadable_not_a_guess() -> None:
    cut_off = LLMResponse(content="<think>\nA is", model="m", finish_reason=FinishReason.LENGTH)
    llm = FakeLLM([cut_off, cut_off])
    assert not (await TextDecider(llm).choose(QUESTION)).answered


async def test_probabilities_are_read_where_the_letter_follows_the_reasoning() -> None:
    tokens = (
        TokenLogprob("<think>", 0.0, (("<think>", 0.0),)),
        TokenLogprob("A?", log(0.9), (("A?", log(0.9)),)),
        TokenLogprob("</think>", 0.0, (("</think>", 0.0),)),
        TokenLogprob("B", log(0.8), (("B", log(0.8)), ("A", log(0.2)))),
    )
    response = LLMResponse(content="<think>A?</think>B", model="m", logprobs=tokens)

    answer = await TextDecider(FakeLLM([response])).choose(QUESTION)

    assert answer.key == "work"
    assert answer.confidence == pytest.approx(0.8)


async def test_a_provider_that_refuses_probabilities_is_asked_without_them_from_then_on() -> None:
    llm = FakeLLM([InvalidRequestError("logprobs not supported"), reply("A"), reply("B")])
    decider = TextDecider(llm)

    first = await decider.choose(QUESTION)
    second = await decider.choose(QUESTION)

    assert (first.key, second.key) == ("reply", "work")
    assert [request.top_logprobs for request in llm.requests] == [5, None, None]


async def test_a_transient_failure_is_not_mistaken_for_a_refusal() -> None:
    with pytest.raises(RateLimitError):
        await TextDecider(FakeLLM([RateLimitError("slow down")])).choose(QUESTION)


# --- The wire -----------------------------------------------------------------------


async def test_probabilities_are_asked_for_and_read_off_the_wire() -> None:
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vendor/small",
                "choices": [
                    {
                        "message": {"content": "A"},
                        "finish_reason": "stop",
                        "logprobs": {
                            "content": [
                                {
                                    "token": "A",
                                    "logprob": -0.1,
                                    "top_logprobs": [
                                        {"token": "A", "logprob": -0.1},
                                        {"token": "B", "logprob": -2.4},
                                    ],
                                }
                            ]
                        },
                    }
                ],
            },
        )

    provider = OpenRouterProvider(
        "key",
        default_model="vendor/small",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(attempts=1),
    )
    response = await provider.generate(
        LLMRequest(messages=(Message.user("?"),), top_logprobs=5)
    )

    assert sent["logprobs"] is True and sent["top_logprobs"] == 5
    assert response.logprobs[0].alternatives == (("A", -0.1), ("B", -2.4))


async def test_nothing_is_asked_for_when_nothing_is_wanted() -> None:
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    provider = OpenRouterProvider(
        "key",
        default_model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(attempts=1),
    )
    response = await provider.generate(LLMRequest(messages=(Message.user("?"),)))

    assert "logprobs" not in sent
    assert response.logprobs == ()


# --- Routing ------------------------------------------------------------------------


def catalog(decision: str = "writer", *, jev_privacy: str = "REMOTE") -> ModelCatalog:
    return ModelCatalog.from_dict(
        {
            "models": {
                "writer": {
                    "provider": "local",
                    "model": "small",
                    "capabilities": ["TEXT_REASONING"],
                },
                "jev": {
                    "provider": "typesafe",
                    "model": "jev",
                    "capabilities": ["DECISION"],
                    "privacy": jev_privacy,
                },
            },
            "defaults": {"extraction": "writer", "decision": decision},
        }
    )


def test_a_model_that_only_decides_is_never_given_text_to_write() -> None:
    models = catalog()
    jev = models.get("jev")

    assert jev.decides and not jev.generates_text
    assert [entry.name for entry in models.candidates(CapabilityRequirement())] == ["writer"]
    assert [
        entry.name
        for entry in models.candidates(
            CapabilityRequirement(required=frozenset({Capability.DECISION}))
        )
    ] == ["jev"]


def test_routing_text_decisions_to_it_falls_back_to_a_text_model() -> None:
    choice = CapabilityAwareModelRouter(catalog("jev")).select(
        TaskKind.DECISION, CapabilityRequirement()
    )
    assert choice.entry == "writer"
    assert "cannot do this work" in choice.reason


async def test_decisions_routed_to_a_text_model_are_asked_as_text() -> None:
    text, typed = FakeDecider([ChoiceAnswer(key="work")]), FakeDecider()
    decider = RoutedDecider(text, catalog=lambda: catalog("writer"), typed=lambda _: typed)

    assert (await decider.choose(QUESTION)).key == "work"
    assert typed.questions == []


async def test_decisions_routed_to_a_decision_model_go_to_it() -> None:
    text, typed = FakeDecider(), FakeDecider([ChoiceAnswer(key="reply", confidence=0.93)])
    chosen: list[ModelChoice] = []

    def build(choice: ModelChoice) -> FakeDecider:
        chosen.append(choice)
        return typed

    answer = await RoutedDecider(text, catalog=lambda: catalog("jev"), typed=build).choose(
        QUESTION
    )

    assert answer.confidence == 0.93
    assert text.questions == []
    assert (chosen[0].provider, chosen[0].model) == ("typesafe", "jev")


async def test_a_decision_model_that_cannot_answer_hands_the_question_to_text() -> None:
    text = FakeDecider([ChoiceAnswer(key="work")])
    typed = FakeDecider([ConfigurationError("not implemented yet")])

    decider = RoutedDecider(text, catalog=lambda: catalog("jev"), typed=lambda _: typed)

    answer = await decider.choose(QUESTION)

    assert answer.key == "work"
    assert len(typed.questions) == 1 and len(text.questions) == 1


async def test_local_only_keeps_decisions_off_a_remote_decision_model() -> None:
    text, typed = FakeDecider([ChoiceAnswer(key="work")]), FakeDecider()
    decider = RoutedDecider(
        text, catalog=lambda: catalog("jev"), typed=lambda _: typed, local_only=True
    )

    await decider.choose(QUESTION)

    assert typed.questions == []


def test_a_decision_model_is_never_built_as_a_text_client() -> None:
    factory = ProviderFactory(catalog=catalog("jev"), api_key="key", base_url="")
    jev = ModelChoice(provider="typesafe", model="jev")

    with pytest.raises(ConfigurationError, match="decision"):
        factory.for_choice(jev)
    with pytest.raises(ConfigurationError):
        factory.for_decisions(ModelChoice(provider="local", model="small"))
    assert factory.for_decisions(jev) is factory.for_decisions(jev), "one client per connection"


def test_a_profile_says_where_decisions_actually_go() -> None:
    models = catalog("jev")
    profile = current_profile(CapabilityAwareModelRouter(models), models)
    assert profile.routes[TaskKind.DECISION.value].startswith("jev ")
