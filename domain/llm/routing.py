"""Why a model was chosen, recorded next to the call it was chosen for.

"Which model did this" had an answer in the call log and "why that one" had an
answer only in a debug line nobody kept. A trace that shows the model and not
the reason cannot tell a person whether a cheap model ran because it was the
configured default, because they picked it, or because nothing stronger fitted -
and Phase 10 is about exactly that distinction.

The decision is carried in a `ContextVar` for the length of one call, set by the
client that routed and read by the client that meters, so neither the adapters
nor the callers learn about it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    task_kind: str
    entry: str
    reason: str
    escalation_level: int = 0
    #: What the entry is allowed to see: LOCAL never leaves this machine.
    privacy: str = ""


_decision: ContextVar[RoutingDecision | None] = ContextVar(
    "prometheus_routing_decision", default=None
)
_task: ContextVar[UUID | None] = ContextVar("prometheus_billed_task", default=None)


def decision() -> RoutingDecision | None:
    return _decision.get()


@contextmanager
def deciding(value: RoutingDecision | None) -> Iterator[None]:
    token = _decision.set(value)
    try:
        yield
    finally:
        _decision.reset(token)


def billed_task() -> UUID | None:
    """The task whose run is making model calls right now, if any."""
    return _task.get()


@contextmanager
def billing(task_id: UUID | None) -> Iterator[None]:
    token = _task.set(task_id)
    try:
        yield
    finally:
        _task.reset(token)
