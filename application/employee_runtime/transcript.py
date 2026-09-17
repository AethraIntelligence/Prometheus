"""The conversation so far, in a form that survives a restart.

Resumability lives or dies here. A run that is killed mid-task has to come back
knowing what it already said, what tools it already called and what came back -
otherwise "resume" means "start again and pay twice".

So the transcript is a value that can be written to the task's `state` column
after every step and read back by a different process.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from domain.integrations.untrusted import Provenance, TrustLevel
from domain.llm.models import Message, Role, ToolCallRequest
from domain.tasks.plan import Observation

# Roughly sixteen thousand tokens for ordinary prose, leaving room for the
# system prompt, tool schemas and the model's answer on common context windows.
# The persisted transcript remains complete; this only bounds one model call.
MODEL_CONTEXT_CHARS = 48_000


def message_to_state(message: Message) -> dict[str, Any]:
    state: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.name:
        state["name"] = message.name
    if message.tool_call_id:
        state["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        state["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
    return state


def message_from_state(raw: dict[str, Any]) -> Message:
    return Message(
        role=Role(raw["role"]),
        content=raw.get("content", ""),
        name=raw.get("name"),
        tool_call_id=raw.get("tool_call_id"),
        tool_calls=tuple(
            ToolCallRequest(
                id=call.get("id", ""),
                name=call.get("name", ""),
                arguments=call.get("arguments", {}),
            )
            for call in raw.get("tool_calls", ())
        ),
    )


@dataclass(frozen=True, slots=True)
class Transcript:
    """Messages, observations and what has been spent, as one resumable value."""

    messages: tuple[Message, ...] = ()
    observations: tuple[Observation, ...] = ()
    cost_usd: float = 0.0
    steps: int = 0

    def with_message(self, *messages: Message) -> Transcript:
        return replace(self, messages=(*self.messages, *messages))

    def with_observation(self, observation: Observation) -> Transcript:
        return replace(self, observations=(*self.observations, observation))

    def with_spend(self, cost_usd: float) -> Transcript:
        return replace(self, cost_usd=self.cost_usd + cost_usd)

    def advanced(self) -> Transcript:
        return replace(self, steps=self.steps + 1)

    def messages_for_model(self, max_chars: int = MODEL_CONTEXT_CHARS) -> tuple[Message, ...]:
        """Opening instructions plus the nearest complete tool exchanges.

        Tool results cannot be detached from the assistant call that requested
        them: several providers reject that wire shape. Old exchanges are
        therefore removed as groups. A newest exchange larger than the whole
        budget is kept with its text shortened, while the durable transcript
        still contains the complete result for audit and resume.
        """
        if len(self.messages) <= 2:
            return self.messages
        opening = self.messages[:2]
        remaining = max(0, max_chars - sum(_message_size(item) for item in opening))
        groups = _exchange_groups(self.messages[2:])
        selected: list[tuple[Message, ...]] = []
        for group in reversed(groups):
            size = sum(_message_size(item) for item in group)
            if size <= remaining:
                selected.append(group)
                remaining -= size
                continue
            if not selected and remaining > 0:
                selected.append(_shortened(group, remaining))
            break
        selected.reverse()
        return (*opening, *(message for group in selected for message in group))

    @property
    def last_observation(self) -> Observation | None:
        return self.observations[-1] if self.observations else None

    @property
    def untrusted_sources(self) -> tuple[Provenance, ...]:
        """External inputs currently capable of influencing the next action.

        Derived from the durable transcript so a restart cannot forget that an
        action was proposed after reading untrusted data.
        """
        found: dict[tuple[str, str], Provenance] = {}
        for observation in self.observations:
            raw = observation.details.get("provenance")
            if not isinstance(raw, dict) or raw.get("trust") != TrustLevel.UNTRUSTED.value:
                continue
            source = str(raw.get("source", "unknown"))
            kind = str(raw.get("kind", "external"))
            found[(source, kind)] = Provenance(source, kind, TrustLevel.UNTRUSTED)
        return tuple(found.values())

    # --- Persistence ----------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {
            "messages": [message_to_state(m) for m in self.messages],
            "observations": [o.to_dict() for o in self.observations],
            "cost_usd": self.cost_usd,
            "steps": self.steps,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any] | None) -> Transcript:
        if not state:
            return cls()
        return cls(
            messages=tuple(message_from_state(m) for m in state.get("messages", ())),
            observations=tuple(
                Observation.from_dict(o) for o in state.get("observations", ())
            ),
            cost_usd=float(state.get("cost_usd", 0.0)),
            steps=int(state.get("steps", 0)),
        )


@dataclass(frozen=True, slots=True)
class RunState:
    """Everything a resumed run needs, including which stage it stopped in."""

    stage: str = "PLANNING"
    transcript: Transcript = field(default_factory=Transcript)
    attempt: int = 1
    verifier_feedback: tuple[str, ...] = ()

    def to_state(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "attempt": self.attempt,
            "verifier_feedback": list(self.verifier_feedback),
            "transcript": self.transcript.to_state(),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any] | None) -> RunState:
        if not state:
            return cls()
        return cls(
            stage=state.get("stage", "PLANNING"),
            transcript=Transcript.from_state(state.get("transcript")),
            attempt=int(state.get("attempt", 1)),
            verifier_feedback=tuple(state.get("verifier_feedback", ())),
        )


def _message_size(message: Message) -> int:
    calls = sum(len(call.name) + len(str(call.arguments)) for call in message.tool_calls)
    return len(message.content) + calls + 32


def _exchange_groups(messages: tuple[Message, ...]) -> list[tuple[Message, ...]]:
    groups: list[list[Message]] = []
    for message in messages:
        if message.role is Role.TOOL and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    return [tuple(group) for group in groups]


def _shortened(group: tuple[Message, ...], budget: int) -> tuple[Message, ...]:
    each = max(80, budget // max(1, len(group)) - 32)
    return tuple(
        replace(
            message,
            content=(
                message.content
                if len(message.content) <= each
                else message.content[: max(0, each - 24)].rstrip() + "\n[output shortened]"
            ),
        )
        for message in group
    )
