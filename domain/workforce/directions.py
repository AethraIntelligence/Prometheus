"""What the person said about *how* to do one request, beside what to do.

Two things, both chosen under the field a request is typed into: whether an
action that needs approval is asked about, done or refused, and which model the
work should prefer. Neither is part of the request's text - a sentence ending
"and don't ask me" is a sentence a model weighs, and §67 is about exactly that -
so they travel beside it, as values something other than a model reads.

**They are carried, not passed.** The approval gate sits below the employee
runtime and the model router below the container, and threading two arguments
through the manager, the supervisor, the task runner and every runtime to reach
them would make every one of those layers know about a choice none of them
makes. A `ContextVar` is what `WorkspaceContext.enter` already uses for the same
problem: set once around one objective, inherited by every task that objective
starts, and invisible to a parallel run in another conversation.

**Neither can widen what the machine allows.** Refusing is always available. Not
asking is available only where this machine would have asked a person anyway -
a machine configured to refuse, or with no approver at all, stays that way. A
chosen model wins only among the models that can do the work: the verifier's
floor and a vision requirement are requirements, and a preference that beat them
would be the failure ADR 0003 was written against.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum


class ApprovalChoice(StrEnum):
    #: Ask a person, which is what the machine does when nobody says otherwise.
    ASK = "ASK"
    #: The person said yes in advance, for this request.
    AUTO = "AUTO"
    #: Refuse without asking. Always allowed: it can only narrow.
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class Directions:
    approvals: ApprovalChoice = ApprovalChoice.ASK
    #: A catalog entry by name. Empty means the router decides, as it always has.
    model: str = ""


NONE = Directions()

_current: ContextVar[Directions] = ContextVar("prometheus_directions", default=NONE)


def current() -> Directions:
    """The directions of the run in front of us, or none outside one."""
    return _current.get()


@contextmanager
def given(directions: Directions) -> Iterator[None]:
    """Carry one run under these directions, and only that run."""
    token = _current.set(directions)
    try:
        yield
    finally:
        _current.reset(token)
