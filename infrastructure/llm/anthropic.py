"""The Messages API, which is not the chat-completions format.

`ChatCompletionsProvider` covers the services that speak one shape. This one
genuinely differs on the wire, and the differences are not cosmetic:

* **The system prompt is a top-level field**, not a message with a role. A
  transcript that begins with a system turn has to be split, not translated.
* **Tool results are content blocks on a user message**, not a `tool` role. Two
  tool results in a row belong to *one* user message; sending them as two turns
  is a different conversation, and the model is trained on the first shape.
* **A tool call is a `tool_use` block with parsed input**, not a function call
  carrying a JSON string that has to be decoded (and can fail to).
* **`max_tokens` is required.** There is no server-side default to fall back on.
* **Usage is `input_tokens` / `output_tokens`**, not prompt/completion.

Three quirks are worth naming because getting them wrong is a 400 rather than a
worse answer. **Tool names may not contain a dot**, and every tool in this
platform is called `fs.read` or `code.run` - so names are translated on the way
out and translated back on the way in, because the name that comes back is what
the executor checks permissions against. Sampling parameters were removed on the
current top models, so `temperature` is sent only to the models that still take
it. And the JSON-object response format has no equivalent here - see `_payload`
for what is done instead.

Raw `httpx` rather than the `anthropic` package, for one reason: every adapter in
this directory translates a wire format into `domain.llm` values, and shares one
retry policy and one error taxonomy. An SDK would bring its own of both, and this
adapter would spend its time turning them back into ours.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any

import httpx

from domain.errors import ProviderError
from domain.llm.models import (
    FinishReason,
    ImageContent,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolCallRequest,
    Usage,
)
from domain.tools.models import ToolSpec
from infrastructure.llm.errors import translate_status, translate_transport_error
from infrastructure.llm.retry import RetryPolicy, with_retry
from infrastructure.llm.tool_names import wire_name
from infrastructure.llm.tool_names import wire_names as shared_wire_names
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

PROVIDER_NAME = "anthropic"
BASE_URL = "https://api.anthropic.com/v1"

#: The wire version this adapter is written against. Sent on every request; the
#: API is explicit that a client which does not pin one is asking for surprises.
API_VERSION = "2023-06-01"

#: Required on every request, so there has to be a default. Chosen high rather
#: than low: a truncated answer costs the tokens it spent *and* a retry, while
#: an unused ceiling costs nothing.
DEFAULT_MAX_TOKENS = 16000

DEFAULT_TIMEOUT_SECONDS = 120.0

#: Models that still accept `temperature`. The current top models removed
#: sampling parameters and answer a request carrying one with a 400, so this is
#: an allow-list rather than a deny-list: a model nobody has checked gets the
#: request that works everywhere.
SAMPLING_MODELS = frozenset(
    {
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-0",
    }
)

_STOP_REASONS = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    # The turn paused and can be resumed. Nothing here resumes one, so it is
    # reported as a normal stop rather than as an error the caller cannot act on.
    "pause_turn": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "refusal": FinishReason.CONTENT_FILTER,
}

_DATA_URL = re.compile(r"^data:(?P<media_type>[^;]+);base64,(?P<data>.+)$", re.DOTALL)



class AnthropicProvider:
    """Implements `domain.llm.protocols.LLM` over `POST /v1/messages`."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        default_model: str = "",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_policy: RetryPolicy | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout_seconds
        self._retry_policy = retry_policy or RetryPolicy()
        self._max_tokens = max_tokens
        self._client = client
        self._owns_client = client is None

    # --- Client lifecycle -----------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # --- LLM ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        payload = self._payload(request)
        started = time.perf_counter()

        async def _call() -> dict[str, Any]:
            return await self._post("/messages", payload)

        body = await with_retry(_call, self._retry_policy)
        # The reverse of the name translation done on the way out. Built from
        # the same source, so a tool the model asked for comes back under the
        # name the executor knows it by.
        real = {
            wire: real_name
            for real_name, wire in wire_names(spec.name for spec in request.tools).items()
        }
        return self._response(
            body, latency_ms=int((time.perf_counter() - started) * 1000), names=real
        )

    # --- Request --------------------------------------------------------------

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model or self._default_model
        names = wire_names(spec.name for spec in request.tools)
        system, messages = _split_system(request.messages, names)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens or self._max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        if request.tools:
            payload["tools"] = [_tool(spec, names) for spec in request.tools]
        if model in SAMPLING_MODELS:
            payload["temperature"] = request.temperature
        # `response_format` is the chat-completions way of asking for JSON and
        # has no equivalent here. It is dropped rather than guessed at: the
        # structured-output feature needs a schema this layer does not have, and
        # every caller that sets it also *says* so in its prompt and parses the
        # reply forgivingly (`domain.llm.json_output`). Silently sending an
        # unknown field would be a 400 for no gain.
        return payload

    # --- Response -------------------------------------------------------------

    def _response(
        self, body: dict[str, Any], *, latency_ms: int, names: dict[str, str] | None = None
    ) -> LLMResponse:
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"{self.provider_name} returned an unusable response: {body}")

        text: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                wire = str(block.get("name", ""))
                tool_calls.append(
                    ToolCallRequest(
                        id=str(block.get("id", "")),
                        name=(names or {}).get(wire, wire),
                        # Already an object on the wire - no JSON string to
                        # decode, and so nothing here that can fail to decode.
                        arguments=block.get("input") or {},
                    )
                )

        stop_reason = body.get("stop_reason")
        if stop_reason == "refusal":
            details = body.get("stop_details") or {}
            log.warning(
                "llm.refused",
                provider=self.provider_name,
                category=details.get("category"),
            )

        usage = body.get("usage") or {}
        return LLMResponse(
            content="\n".join(part for part in text if part),
            model=body.get("model", self._default_model),
            tool_calls=tuple(tool_calls),
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                latency_ms=latency_ms,
            ),
            finish_reason=_STOP_REASONS.get(str(stop_reason), FinishReason.STOP),
        )

    # --- HTTP -----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        try:
            response = await client.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except Exception as error:  # httpx transport failures
            raise translate_transport_error(error, provider=self.provider_name) from error

        if response.status_code >= 400:
            raise translate_status(
                response.status_code, response.text, provider=self.provider_name
            )

        try:
            return response.json()
        except ValueError as error:
            raise ProviderError(
                f"{self.provider_name} returned a body that is not JSON: {response.text[:200]}"
            ) from error


# --- Translating a transcript -------------------------------------------------


def wire_names(names: Any) -> dict[str, str]:
    """This API's longer limit on the shared translation (`tool_names.py`)."""
    return shared_wire_names(names, max_length=128)


def _split_system(
    messages: tuple[Message, ...], names: dict[str, str] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Lift the system turns out, and turn the rest into Messages-API turns.

    Two things happen here that a naive per-message mapping gets wrong.

    Consecutive tool results become **one** user message. The API models a turn
    as "everything that came back before the assistant spoke again", and sending
    one message per result describes a different conversation - one where the
    assistant was asked three times in a row.

    A run of same-role turns is merged for the same reason, and because a
    transcript that alternates strictly is what every example is written
    against.
    """
    system: list[str] = []
    turns: list[dict[str, Any]] = []

    for message in messages:
        if message.role is Role.SYSTEM:
            if message.content.strip():
                system.append(message.content)
            continue

        role = "user" if message.role in (Role.USER, Role.TOOL) else "assistant"
        content = _content(message, names or {})
        if not content:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(content)
        else:
            turns.append({"role": role, "content": content})

    return "\n\n".join(system), turns


def _content(message: Message, names: dict[str, str]) -> list[dict[str, Any]]:
    if message.role is Role.TOOL:
        return [
            {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.content,
            }
        ]

    blocks: list[dict[str, Any]] = []
    if message.content.strip():
        blocks.append({"type": "text", "text": message.content})
    for image in message.images:
        block = _image(image)
        if block is not None:
            blocks.append(block)
    for call in message.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                # A replayed transcript names tools the same way the request
                # does, or the model is shown a history it could not have made.
                "name": wire_name(call.name, names, max_length=128),
                "input": call.arguments,
            }
        )
    return blocks


def _image(image: ImageContent) -> dict[str, Any] | None:
    """Inline base64, which is the only form this platform ever sends.

    The pictures here are screenshots of the user's own machine; a URL would
    mean putting one somewhere to be fetched back.
    """
    match = _DATA_URL.match(image.data_url)
    if match is None:
        log.warning("llm.image_not_inline", provider=PROVIDER_NAME)
        return None
    data = match.group("data")
    try:
        base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        log.warning("llm.image_not_base64", provider=PROVIDER_NAME)
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": match.group("media_type"),
            "data": data,
        },
    }


def _tool(spec: ToolSpec, names: dict[str, str]) -> dict[str, Any]:
    """A flat tool, not one wrapped in a `function` object."""
    return {
        "name": names.get(spec.name, spec.name),
        "description": spec.description,
        "input_schema": spec.json_schema or {"type": "object", "properties": {}},
    }
