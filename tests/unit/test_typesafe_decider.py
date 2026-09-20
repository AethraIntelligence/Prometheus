"""The TypeSafe adapter, over a mock transport. No socket is ever opened.

The request is checked against the documented shape (`POST /v1/systemone`, a
state, a map of typed questions) and the answer against what the platform must
never do with it: take an option the question did not offer, invent a
confidence, or lose track of what the call cost.
"""

from __future__ import annotations

import json

import httpx
import pytest

from domain.decisions.models import ChoiceQuestion, Option
from domain.errors import InvalidRequestError, ProviderUnavailableError
from domain.llm.telemetry import LLMCallRecord
from infrastructure.decisions.typesafe import TypeSafeDecider
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.retry import RetryPolicy

QUESTION = ChoiceQuestion(
    state="What is the weather in Milan now?",
    question="Reply from what you know, or go and find out?",
    options=(Option("reply", "A reply."), Option("work", "Something to find out.")),
    purpose="triage",
)

ANSWER = {
    "model": "jev-1.13.0",
    "answers": {
        "decision": {
            "type": "choice",
            "choice": "work",
            "probabilities": {"reply": 0.1, "work": 0.9},
            "confidence": 0.86,
        }
    },
    "usage": {"input_tokens": 1_000_000, "output_tokens": 48},
}

CATALOG = ModelCatalog.from_dict(
    {
        "models": {
            "jev": {
                "provider": "typesafe",
                "model": "jev-latest",
                "capabilities": ["DECISION"],
                "input_cost_per_1k_usd": 0.000042,
            }
        }
    }
)


class Calls:
    def __init__(self) -> None:
        self.records: list[LLMCallRecord] = []

    async def record(self, record: LLMCallRecord) -> None:
        self.records.append(record)


def decider(handler, *, base_url: str = "", calls: Calls | None = None, attempts: int = 1):
    return TypeSafeDecider(
        "ts-key",
        model="jev-latest",
        base_url=base_url,
        entry="jev",
        catalog=CATALOG,
        call_log=calls,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(attempts=attempts, base_delay_seconds=0),
    )


async def test_a_choice_is_sent_in_the_documented_shape_and_read_back() -> None:
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["auth"] = request.headers["Authorization"]
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json=ANSWER)

    answer = await decider(handler).choose(QUESTION)

    assert sent["url"] == "https://api.typesafe.ai/v1/systemone"
    assert sent["auth"] == "Bearer ts-key"
    assert sent["body"] == {
        "state": QUESTION.state,
        "model": "jev-latest",
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": QUESTION.question,
                "criteria": {"reply": "A reply.", "work": "Something to find out."},
            }
        },
    }
    assert answer.key == "work"
    assert answer.confidence == 0.86, "its own calibrated confidence, passed through"
    assert answer.probabilities == {"reply": 0.1, "work": 0.9}
    assert answer.source == "decision model"


@pytest.mark.parametrize(
    "typed", ["https://api.typesafe.ai", "https://api.typesafe.ai/", "https://api.typesafe.ai/v1"]
)
async def test_the_address_is_accepted_with_or_without_the_version(typed: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=ANSWER)

    await decider(handler, base_url=typed).choose(QUESTION)

    assert seen == ["https://api.typesafe.ai/v1/systemone"]


async def test_an_option_that_was_not_offered_is_unreadable() -> None:
    body = json.loads(json.dumps(ANSWER))
    body["answers"]["decision"]["choice"] = "maybe"

    answer = await decider(lambda _: httpx.Response(200, json=body)).choose(QUESTION)

    assert not answer.answered


@pytest.mark.parametrize("confidence", [None, "high", 1.7])
async def test_a_missing_or_impossible_confidence_is_none(confidence) -> None:
    body = json.loads(json.dumps(ANSWER))
    if confidence is None:
        del body["answers"]["decision"]["confidence"]
    else:
        body["answers"]["decision"]["confidence"] = confidence

    answer = await decider(lambda _: httpx.Response(200, json=body)).choose(QUESTION)

    assert answer.key == "work" and answer.confidence is None


async def test_every_call_is_metered_at_the_routed_entrys_price() -> None:
    """An alias answers under a versioned id the catalog does not list."""
    calls = Calls()

    await decider(lambda _: httpx.Response(200, json=ANSWER), calls=calls).choose(QUESTION)

    [record] = calls.records
    assert record.success and record.provider == "typesafe"
    assert record.model == "jev-1.13.0"
    assert record.task_kind == "DECISION" and record.entry == "jev"
    assert record.usage.cost_usd == pytest.approx(0.042), "a million input tokens at $42/Btok"


async def test_overloaded_is_retried_and_a_bad_key_is_not() -> None:
    answers = iter([httpx.Response(529, text="overloaded"), httpx.Response(200, json=ANSWER)])
    answer = await decider(lambda _: next(answers), attempts=2).choose(QUESTION)
    assert answer.key == "work"

    calls = Calls()
    rejected = decider(lambda _: httpx.Response(401, text="bad key"), calls=calls, attempts=3)
    with pytest.raises(InvalidRequestError):
        await rejected.choose(QUESTION)
    assert [record.success for record in calls.records] == [False]


async def test_an_outage_is_a_transient_provider_error() -> None:
    with pytest.raises(ProviderUnavailableError):
        await decider(lambda _: httpx.Response(503, text="down")).choose(QUESTION)
