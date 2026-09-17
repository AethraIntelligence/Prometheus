"""What an employee needs to be handed, and what it hands back.

A role card says what somebody is for; it does not say whether the work one
person finishes is work the next person can start from. Phase 12 made results
travel along declared edges, and nothing checked that what travelled was usable:
a plan could put an employee that only changes a folder in front of one that
can only write from findings, and the mismatch surfaced as a downstream run that
spent its budget discovering it had nothing to work from.

So a declaration may state a contract, in a closed vocabulary for the same reason
capabilities are closed: a product name nobody else understands matches nothing.

Old declarations carry no contract and must keep loading. Their defaults are the
ones that change nothing about how they were routed before - accepts anything,
delivers an answer, requires no particular evidence, may fail in any way - and
`declared` is False, so a profile can say "not declared" instead of presenting a
default as though somebody had written it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class WorkProduct(StrEnum):
    """The kinds of thing a finished task can hand to the next one."""

    #: A reply in prose. Every task produces one; it is the floor.
    ANSWER = "ANSWER"
    #: Facts gathered or computed, stated in the result for somebody to use.
    FINDINGS = "FINDINGS"
    #: A file written to the workspace.
    FILE = "FILE"
    #: Something in the world rearranged - files moved, a page submitted.
    CHANGES = "CHANGES"


class EvidenceKind(StrEnum):
    """What the record must show before a result counts as delivered."""

    #: At least one tool call reached the world and succeeded.
    TOOL_RESULT = "TOOL_RESULT"
    #: At least one file was written, read off the tool calls rather than the answer.
    ARTIFACT = "ARTIFACT"


class FailureKind(StrEnum):
    """How a task of this role is allowed to end without a result.

    The same distinctions the supervisor recovers by, named once so a contract
    and a statistic count the same things.
    """

    REFUSED = "REFUSED"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    BUDGET = "BUDGET"
    TRANSIENT = "TRANSIENT"
    EXECUTION = "EXECUTION"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class WorkContract:
    """The hand-off terms of one role."""

    #: What it can start from when a task depends on earlier work. Empty means
    #: anything, which is the only reading that keeps an old declaration routable.
    accepts: frozenset[WorkProduct] = field(default_factory=frozenset)
    #: What it delivers. ANSWER is always among them: every task ends in a reply.
    produces: frozenset[WorkProduct] = field(
        default_factory=lambda: frozenset({WorkProduct.ANSWER})
    )
    #: What the record must show for a finished task to be accepted.
    evidence: frozenset[EvidenceKind] = field(default_factory=frozenset)
    #: Failure kinds this role is expected to produce. Anything else in its
    #: history is worth a person's attention; the default expects all of them.
    failure_kinds: frozenset[FailureKind] = field(default_factory=lambda: frozenset(FailureKind))
    #: False when the declaration said nothing and these are the defaults.
    declared: bool = False

    def __post_init__(self) -> None:
        if WorkProduct.ANSWER not in self.produces:
            object.__setattr__(self, "produces", self.produces | {WorkProduct.ANSWER})

    def can_start_from(self, delivered: Iterable[WorkProduct]) -> bool:
        """Whether upstream work that delivered these is something this role can use."""
        if not self.accepts:
            return True
        return bool(self.accepts & frozenset(delivered))


#: The contract of a declaration that states none.
UNDECLARED = WorkContract()
