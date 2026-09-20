"""Provider-neutral request and response values.

No vendor name appears in this module, or anywhere else under `domain/`.
Adapters in `infrastructure/llm/` translate to and from these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.tools.models import ToolSpec


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """A tool invocation the model asked for."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImageContent:
    """A picture put in front of the model, inline.

    Inline rather than a URL: the images this platform sends are screenshots of
    the user's own machine, and uploading one somewhere to be fetched back would
    put it on a network it never needed to touch.
    """

    data_url: str
    #: What the picture is of, for a provider or a log that shows text only.
    alt_text: str = ""


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    #: Attached to a user message. An adapter for a provider that cannot take
    #: images is expected to fail loudly rather than send the text alone.
    images: tuple[ImageContent, ...] = ()
    #: Set on an assistant message that asked for tools, so a follow-up request
    #: can replay the exchange the provider expects.
    tool_calls: tuple[ToolCallRequest, ...] = ()

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(Role.SYSTEM, content)

    @classmethod
    def user(cls, content: str, images: tuple[ImageContent, ...] = ()) -> Message:
        return cls(Role.USER, content, images=images)

    @classmethod
    def assistant(cls, content: str, tool_calls: tuple[ToolCallRequest, ...] = ()) -> Message:
        return cls(Role.ASSISTANT, content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, content: str, tool_call_id: str) -> Message:
        return cls(Role.TOOL, content, tool_call_id=tool_call_id)


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class TaskKind(StrEnum):
    """What a call is for. The router maps this onto a model choice."""

    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    VERIFICATION = "VERIFICATION"
    SYNTHESIS = "SYNTHESIS"
    EXTRACTION = "EXTRACTION"
    CONVERSATION = "CONVERSATION"
    #: Turning text into a vector. A kind of work rather than a kind of model,
    #: for the same reason the others are: the catalog says which entry does it.
    EMBEDDING = "EMBEDDING"
    #: A typed question with a closed set of answers (`domain/decisions/`).
    #: Its own kind so a person can send it somewhere other than extraction -
    #: a small fast model, or a model built to decide rather than to write.
    DECISION = "DECISION"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """What an employee needs from a model, never which vendor to use."""

    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset({Capability.TEXT_REASONING})
    )
    min_context_tokens: int | None = None
    max_cost_per_1k_usd: float | None = None
    temperature: float = 0.2

    def as_requirement(self) -> CapabilityRequirement:
        return CapabilityRequirement(
            required=self.capabilities, min_context_tokens=self.min_context_tokens
        )


@dataclass(frozen=True, slots=True)
class RoutingHints:
    quality: float = 0.5
    latency_sensitivity: float = 0.5
    cost_sensitivity: float = 0.5
    context_tokens: int | None = None
    needs_tools: bool = False


@dataclass(frozen=True, slots=True)
class ModelChoice:
    provider: str
    model: str
    reason: str = ""
    #: Which configured way in to that provider to use, by name. Empty means
    #: the one the machine was configured with - which is what every choice
    #: meant before a person could add a second account to one provider.
    connection: str = ""
    #: The catalog entry chosen, by name, and how many steps above the normal
    #: choice it is. Both are for the trace; the adapters read neither.
    entry: str = ""
    escalation_level: int = 0
    privacy: str = ""


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """What the platform asks of a model.

    `tools` carries domain tool specs, not a provider's JSON shape: turning them
    into whatever the wire format wants is the adapter's job.
    """

    messages: tuple[Message, ...]
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    temperature: float = 0.2
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None
    #: How many alternatives to report per generated token, where the provider
    #: can. None asks for nothing, which is every request but a decision's.
    top_logprobs: int | None = None


@dataclass(frozen=True, slots=True)
class TokenLogprob:
    """One generated token and the likeliest alternatives at that position."""

    token: str
    logprob: float
    alternatives: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    model: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.STOP
    #: Filled only when asked for and the provider reported them. Empty means
    #: "not measured", never "improbable".
    logprobs: tuple[TokenLogprob, ...] = ()
