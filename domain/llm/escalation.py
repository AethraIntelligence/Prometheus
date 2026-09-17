"""When a task failed because the model was not good enough, try a better one.

Routing picks the cheapest model the catalog says can do a piece of work, which
is the right default and is wrong exactly when the work turned out harder than
its kind suggested. The obvious fix - route everything to the strongest model -
spends the most expensive tokens on the easiest work. So a stronger model is
asked for *after* a failure, and only after a failure of a kind a stronger
model plausibly fixes.

**The cause is classified, never read from text.** A verifier that rejected the
result, a plan that could not be produced, a step budget spent without finishing,
a result the manager would not accept on the evidence: those are the model's
failures. A refused tool, a person's no, a cost budget, a cancellation, a
provider outage are not, and a stronger model would meet the same wall at a
higher price - so they never escalate.

**Carried like directions.** The level travels in a `ContextVar` for the one run
it belongs to, and the router reads it; nothing below needs to know a retry is
an escalation.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from domain.capabilities.models import CapabilityRequirement
from domain.llm.models import TaskKind

#: How many steps up one task may be sent. One: a second escalation is a third
#: model conversation about the same instruction, and a plan that needs it is a
#: plan to reconsider rather than a model to upgrade again.
MAX_LEVEL = 1


class EscalationCause(StrEnum):
    VERIFICATION_REJECTED = "VERIFICATION_REJECTED"
    PLANNING_FAILED = "PLANNING_FAILED"
    STEP_BUDGET = "STEP_BUDGET"
    NOT_ACCEPTED = "NOT_ACCEPTED"


@dataclass(frozen=True, slots=True)
class Escalation:
    level: int = 0
    cause: EscalationCause | None = None

    @property
    def active(self) -> bool:
        return self.level > 0

    def to_dict(self) -> dict[str, object]:
        return {"level": self.level, "cause": self.cause.value if self.cause else ""}

    @classmethod
    def from_dict(cls, raw: object) -> Escalation:
        if not isinstance(raw, dict):
            return NONE
        try:
            level = max(int(raw.get("level", 0)), 0)
            cause = EscalationCause(raw["cause"]) if raw.get("cause") else None
        except (TypeError, ValueError):
            return NONE
        return cls(level=min(level, MAX_LEVEL), cause=cause)


NONE = Escalation()

_current: ContextVar[Escalation] = ContextVar("prometheus_escalation", default=NONE)


def current() -> Escalation:
    return _current.get()


@contextmanager
def given(escalation: Escalation) -> Iterator[None]:
    token = _current.set(escalation)
    try:
        yield
    finally:
        _current.reset(token)


def cause_of(
    *,
    completed: bool,
    error_kind: str = "",
    stopped_by: str | None = None,
    refused: bool = False,
    accepted: bool = True,
) -> EscalationCause | None:
    """Whether this failure is one a stronger model plausibly fixes, and which.

    Read from the task's own record: its error type, the limit that stopped it,
    and the manager's acceptance. A cost or wall-time limit is deliberately not
    a cause - a stronger model is slower and dearer, and would stop sooner.
    """
    if refused:
        return None
    if completed:
        return None if accepted else EscalationCause.NOT_ACCEPTED
    if stopped_by == "STEPS":
        return EscalationCause.STEP_BUDGET
    if stopped_by:
        return None
    if error_kind in ("VerificationFailed", "VerificationFailedError"):
        return EscalationCause.VERIFICATION_REJECTED
    if error_kind == "PlanningError":
        return EscalationCause.PLANNING_FAILED
    return None


class EscalationAdvisor(Protocol):
    """Whether stepping up would change the model at all.

    The router answers, because only it knows the catalog. A retry that would
    run on the same model is a repeat - the retry policy's business, not this.
    """

    def stronger_available(
        self, task_kind: TaskKind, required: CapabilityRequirement | None = None
    ) -> bool: ...
