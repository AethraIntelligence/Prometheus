"""The adapter, exercised over a mock transport. No socket is ever opened."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from domain.capabilities.models import Capability
from domain.errors import (
    InvalidRequestError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TimeoutError,
)
from domain.llm.models import FinishReason, LLMRequest, Message, ToolCallRequest
from domain.tools.models import ToolSpec
from infrastructure.llm.openrouter import OpenRouterProvider
from infrastructure.llm.retry import RetryPolicy

COMPLETION = {
    "model": "vendor/medium",
    "choices": [{"message": {"content": "Berlin."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
}


def provider(handler, **kwargs) -> OpenRouterProvider:
    return OpenRouterProvider(
        "test-key",
        default_model="vendor/medium",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(attempts=1),
        **kwargs,
    )


def ask(text: str = "Which city?") -> LLMRequest:
    return LLMRequest(messages=(Message.user(text),))


async def test_a_plain_answer_round_trips() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=COMPLETION)

    response = await provider(handler).generate(ask())

    assert response.content == "Berlin."
    assert response.model == "vendor/medium"
    assert response.usage.prompt_tokens == 12
    assert response.usage.output_tokens == 3
    assert response.usage.latency_ms >= 0
    assert response.finish_reason is FinishReason.STOP
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["messages"] == [{"role": "user", "content": "Which city?"}]


async def test_domain_tool_specs_become_the_provider_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=COMPLETION)

    tool = ToolSpec(
        name="browser.search",
        description="Search the web",
        json_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        capabilities=frozenset({Capability.WEB_BROWSING}),
    )
    await provider(handler).generate(LLMRequest(messages=(Message.user("hi"),), tools=(tool,)))

    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                # Translated: a validating provider refuses a dot outright.
                "name": "browser_search",
                "description": "Search the web",
                "parameters": tool.json_schema,
            },
        }
    ]


async def test_tool_calls_are_parsed_back_into_domain_values() -> None:
    body = {
        "model": "vendor/medium",
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "browser.search",
                                "arguments": '{"query": "berlin"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    }
    response = await provider(lambda _r: httpx.Response(200, json=body)).generate(ask())

    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.content == ""
    assert response.tool_calls == (
        ToolCallRequest(id="call_1", name="browser.search", arguments={"query": "berlin"}),
    )


async def test_malformed_tool_arguments_are_surfaced_as_data() -> None:
    # Models do emit broken argument JSON. Failing the whole response would lose
    # the turn; the executor can ask for a correction instead.
    body = {
        "model": "vendor/medium",
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "c", "function": {"name": "fs.read", "arguments": "{not json"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    response = await provider(lambda _r: httpx.Response(200, json=body)).generate(ask())
    assert response.tool_calls[0].arguments == {"__raw": "{not json"}


async def test_a_tool_result_turn_is_sent_back_in_the_expected_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=COMPLETION)

    call = ToolCallRequest(id="call_1", name="fs.read", arguments={"path": "a.txt"})
    await provider(handler).generate(
        LLMRequest(
            messages=(
                Message.user("read a.txt"),
                Message.assistant("", tool_calls=(call,)),
                Message.tool("contents", tool_call_id="call_1"),
            )
        )
    )

    assistant, tool = captured["body"]["messages"][1], captured["body"]["messages"][2]
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'
    assert tool == {"role": "tool", "content": "contents", "tool_call_id": "call_1"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, RateLimitError),
        (408, TimeoutError),
        (401, InvalidRequestError),
        (400, InvalidRequestError),
        (500, ProviderUnavailableError),
        (503, ProviderUnavailableError),
    ],
)
async def test_provider_statuses_map_onto_the_domain_taxonomy(status, expected) -> None:
    with pytest.raises(expected):
        await provider(lambda _r: httpx.Response(status, text="upstream said no")).generate(ask())


async def test_a_transport_timeout_is_a_domain_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    with pytest.raises(TimeoutError):
        await provider(handler).generate(ask())


async def test_an_unreachable_host_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(ProviderUnavailableError):
        await provider(handler).generate(ask())


async def test_an_unusable_body_is_a_provider_error() -> None:
    with pytest.raises(ProviderError, match="unusable"):
        await provider(lambda _r: httpx.Response(200, json={"choices": []})).generate(ask())


async def test_a_non_json_body_is_a_provider_error() -> None:
    with pytest.raises(ProviderError, match="not JSON"):
        await provider(lambda _r: httpx.Response(200, text="<html>gateway</html>")).generate(ask())


async def test_an_upstream_failure_relayed_in_a_200_is_transient_and_retried() -> None:
    """An overloaded free model used to end a scheduled run in three seconds."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                200,
                json={
                    "id": "gen-1",
                    "error": {
                        "message": "Upstream error: Service temporarily overloaded",
                        "code": 502,
                        "metadata": {"error_type": "provider_unavailable"},
                    },
                },
            )
        return httpx.Response(200, json=COMPLETION)

    retried = OpenRouterProvider(
        "test-key",
        default_model="vendor/medium",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(attempts=2, base_delay_seconds=0),
    )

    response = await retried.generate(ask())

    assert response.content == "Berlin."
    assert calls["count"] == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"message": "overloaded", "code": 502}, ProviderUnavailableError),
        ({"message": "slow down", "code": 429}, RateLimitError),
        ({"message": "bad model id", "code": 400}, InvalidRequestError),
        ({"message": "no code at all"}, ProviderUnavailableError),
    ],
)
async def test_a_failure_in_the_body_keeps_its_kind(error, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": error})

    with pytest.raises(expected):
        await provider(handler).generate(ask())


async def test_dotted_tool_names_go_out_translated_and_come_back_as_declared() -> None:
    """A provider that validates names refused every call carrying `browser.extract`."""
    captured: dict = {}
    extract = ToolSpec(name="browser.extract", description="Extract", json_schema={})
    read = ToolSpec(name="fs.read", description="Read", json_schema={})

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "vendor/medium",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {"name": "browser_extract", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )

    earlier = ToolCallRequest(id="call_1", name="fs.read", arguments={"path": "a.txt"})
    response = await provider(handler).generate(
        LLMRequest(
            messages=(
                Message.user("read, then extract"),
                Message.assistant("", tool_calls=(earlier,)),
                Message.tool("contents", tool_call_id="call_1"),
            ),
            tools=(extract, read),
        )
    )

    sent = [tool["function"]["name"] for tool in captured["body"]["tools"]]
    assert sent == ["browser_extract", "fs_read"]
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) for name in sent)
    replayed = captured["body"]["messages"][1]["tool_calls"][0]["function"]["name"]
    assert replayed == "fs_read", "the history uses the names the request does"
    assert response.tool_calls[0].name == "browser.extract"
