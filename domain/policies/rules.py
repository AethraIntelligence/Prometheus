"""The engine itself: named rules, and the order they are applied in.

Two kinds of rule meet here and they are not interchangeable.

The **threshold** applies to everybody and comes from the risk table alone: at
HIGH and above a person decides. It is the floor, and no declaration removes it
- an employee cannot opt out of being asked about.

A **named policy** is what one employee's declaration opted *into*, and it can
only narrow. `read_only` denies what the threshold would have allowed;
nothing in this catalog widens anything, because a declaration that could grant
itself more than the platform's default would make the default meaningless.

Rules are ordered by severity, not by declaration order: DENY beats
REQUIRE_APPROVAL beats ALLOW whichever policy produced it. Otherwise two
policies on one employee would resolve by the accident of how the YAML was
typed, and a reviewer would have to read the file in order to know what it does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from domain.policies.engine import PolicyRequest
from domain.policies.models import Decision, PolicyDecision, RiskLevel
from domain.policies.risk import Effect, at_least

#: At and above this level, an action waits for a person. §70.
APPROVAL_THRESHOLD = RiskLevel.HIGH

_SEVERITY: dict[Decision, int] = {
    Decision.ALLOW: 0,
    Decision.REQUIRE_APPROVAL: 1,
    Decision.DENY: 2,
}


class PolicyCategory(StrEnum):
    """§69. The category is documentation, not behaviour - it is what a person
    reads in `prometheus policies` to see why a rule exists."""

    SECURITY = "SECURITY"
    FINANCIAL = "FINANCIAL"
    COMMUNICATION = "COMMUNICATION"
    COMPLIANCE = "COMPLIANCE"


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """A named rule an employee declaration can opt into.

    `decide` returns None when the rule has no opinion about this call, which is
    the common case: a rule about spending says nothing about reading a file.
    """

    name: str
    category: PolicyCategory
    description: str
    decide: Callable[[PolicyRequest], PolicyDecision | None]


def _subject(request: PolicyRequest) -> str:
    """What to call this action in a sentence a person reads.

    The tool's name, because `action` is the whole call with its arguments in
    it - and a reason that repeats the action it is a reason for is a line
    nobody finishes reading.
    """
    return request.tool or request.action


def _deny_effects(
    name: str,
    category: PolicyCategory,
    description: str,
    effects: frozenset[Effect],
    reason: str,
) -> PolicyRule:
    def decide(request: PolicyRequest) -> PolicyDecision | None:
        if request.effect not in effects:
            return None
        return PolicyDecision(
            decision=Decision.DENY, reason=reason, risk_level=request.risk_level
        )

    return PolicyRule(name=name, category=category, description=description, decide=decide)


def _irreversible(request: PolicyRequest) -> PolicyDecision | None:
    if request.reversible and not at_least(request.risk_level, APPROVAL_THRESHOLD):
        return None
    return PolicyDecision(
        decision=Decision.REQUIRE_APPROVAL,
        reason=request.risk_reason
        or f"{_subject(request)} cannot be undone and this employee asks before acting",
        risk_level=request.risk_level,
    )


def _approve_every_write(request: PolicyRequest) -> PolicyDecision | None:
    if request.effect is Effect.READ:
        return None
    return PolicyDecision(
        decision=Decision.REQUIRE_APPROVAL,
        reason=request.risk_reason
        or f"{_subject(request)} changes something and this employee asks first",
        risk_level=request.risk_level,
    )


CATALOG: dict[str, PolicyRule] = {
    rule.name: rule
    for rule in (
        PolicyRule(
            name="no_irreversible_actions",
            category=PolicyCategory.SECURITY,
            description="Anything that cannot be undone waits for the user.",
            decide=_irreversible,
        ),
        PolicyRule(
            name="approve_every_write",
            category=PolicyCategory.SECURITY,
            description="Even a reversible change waits for the user.",
            decide=_approve_every_write,
        ),
        _deny_effects(
            "read_only",
            PolicyCategory.SECURITY,
            "May observe the world and change nothing in it.",
            frozenset(set(Effect) - {Effect.READ}),
            "this employee is declared read-only",
        ),
        _deny_effects(
            "no_spending",
            PolicyCategory.FINANCIAL,
            "May not move money. §69, financial.",
            frozenset({Effect.SPEND}),
            "this employee may not spend money",
        ),
        _deny_effects(
            "no_deleting",
            PolicyCategory.SECURITY,
            "May not destroy anything.",
            frozenset({Effect.DELETE}),
            "this employee may not delete anything",
        ),
        _deny_effects(
            "no_sending",
            PolicyCategory.COMMUNICATION,
            "Nothing addressed to a person leaves the machine. §69, communication.",
            frozenset({Effect.SEND}),
            "this employee may not send anything",
        ),
        _deny_effects(
            "no_publishing",
            PolicyCategory.COMPLIANCE,
            "Nothing becomes visible to people who were not asked. §69, compliance.",
            frozenset({Effect.PUBLISH}),
            "this employee may not publish anything",
        ),
    )
}


def known(name: str) -> bool:
    return name in CATALOG


class RuleBasedPolicyEngine:
    """Implements `domain.policies.engine.PolicyEngine`. Pure by construction.

    It reads the request and the catalog and nothing else - no clock, no
    settings, no database - so the same action decided twice decides the same
    way, which is what makes an audit line worth reading.
    """

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        # Least privilege is not decided here. `ToolRegistry` already refuses a
        # tool the actor was never granted, and it does so before the call is
        # built - so a second check here could only ever disagree with the first
        # one, and the disagreement would be silent.
        decisions = [
            decision if decision.source else replace(decision, source=name)
            for name in sorted(request.policies)
            if (rule := CATALOG.get(name)) is not None
            and (decision := rule.decide(request)) is not None
        ]
        decisions.append(self._threshold(request))
        return max(decisions, key=lambda decision: _SEVERITY[decision.decision])

    @staticmethod
    def _threshold(request: PolicyRequest) -> PolicyDecision:
        if at_least(request.risk_level, APPROVAL_THRESHOLD):
            return PolicyDecision(
                decision=Decision.REQUIRE_APPROVAL,
                reason=request.risk_reason
                or f"{_subject(request)} is a {request.risk_level.value.lower()}-risk action",
                risk_level=request.risk_level,
                source="risk_threshold",
            )
        return PolicyDecision(
            decision=Decision.ALLOW,
            reason=request.risk_reason,
            risk_level=request.risk_level,
            source="risk_threshold",
        )
