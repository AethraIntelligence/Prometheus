"""Policy vocabulary.

Policies apply to every actor, Prometheus included. The manager is not exempt from the
rules it enforces on the employees it delegates to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ActorKind(StrEnum):
    USER = "USER"
    PROMETHEUS = "PROMETHEUS"
    EMPLOYEE = "EMPLOYEE"
    SYSTEM = "SYSTEM"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Decision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


@runtime_checkable
class Actor(Protocol):
    """Anything that can request an action: the user, Prometheus, or an employee."""

    @property
    def actor_id(self) -> str: ...

    @property
    def actor_kind(self) -> ActorKind: ...

    @property
    def allowed_tools(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class SimpleActor:
    """A plain actor value, useful for the user, for Prometheus and in tests."""

    actor_id: str
    actor_kind: ActorKind
    allowed_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    reason: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    source: str = ""

    @property
    def is_allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass(frozen=True, slots=True)
class Policy:
    """A named rule an employee definition can opt into.

    The full engine lands in Phase 10; the vocabulary is fixed now so employee
    declarations written earlier stay valid.
    """

    name: str
    description: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False


def effective_tools(delegator: Actor, executor: Actor) -> frozenset[str]:
    """Delegation never escalates privileges: rights are the intersection.

    A task Prometheus hands to an employee is checked against the employee's own
    permissions exactly as if the user had asked the employee directly.
    """
    return delegator.allowed_tools & executor.allowed_tools
