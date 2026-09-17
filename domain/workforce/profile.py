"""One read model of a role: what it is, what it may do, and whether it can now.

Assembled from the declaration and from facts about this machine, never stored.
A stored profile would be a second copy of the declaration that goes stale the
moment somebody edits a YAML file or connects a service; the registry is the
source, and this is how it reads to a person.

Every interface renders this and adds nothing. A tool's "denied by policy" and
an integration's "not connected" are computed here, once, from the same rules
the gate and the delegator apply - a window deciding which of its tools look
dangerous would be a second policy engine (ADR 0014).
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.capabilities.models import Capability
from domain.employees.contract import WorkContract
from domain.employees.definition import EmployeeDefinition
from domain.employees.limits import ExecutionLimits
from domain.memory.models import MemoryScope
from domain.policies.risk import Effect, at_least, risk_of
from domain.policies.rules import APPROVAL_THRESHOLD
from domain.policies.rules import CATALOG as POLICY_CATALOG
from domain.workforce.readiness import Readiness, ReadinessFacts, denied_effects


@dataclass(frozen=True, slots=True)
class ToolAccess:
    name: str
    #: None where this machine does not offer the tool, so its effect is unknown.
    effect: Effect | None
    available: bool
    capabilities: frozenset[Capability]
    #: A declared policy refuses every call of it.
    denied_by_policy: bool
    #: At or above the approval threshold: a person is asked each time.
    asks_first: bool


@dataclass(frozen=True, slots=True)
class IntegrationAccess:
    name: str
    #: Named in the employee's own file, rather than granted from the window.
    declared: bool
    connected: bool


@dataclass(frozen=True, slots=True)
class PolicyAccess:
    name: str
    description: str
    denies: frozenset[Effect]


@dataclass(frozen=True, slots=True)
class ModelNeeds:
    capabilities: frozenset[Capability]
    min_context_tokens: int | None
    max_cost_per_1k_usd: float | None
    temperature: float


@dataclass(frozen=True, slots=True)
class EmployeeProfile:
    id: str
    name: str
    title: str
    description: str
    #: The declaration's version: a digest that changes when the file does.
    version: str
    enabled: bool
    goals: tuple[str, ...]
    capabilities: frozenset[Capability]
    tools: tuple[ToolAccess, ...]
    integrations: tuple[IntegrationAccess, ...]
    policies: tuple[PolicyAccess, ...]
    model: ModelNeeds
    memory_scope: MemoryScope
    limits: ExecutionLimits
    contract: WorkContract
    readiness: Readiness


def build(
    definition: EmployeeDefinition, facts: ReadinessFacts, readiness: Readiness
) -> EmployeeProfile:
    denied = denied_effects(definition.policies)
    declared = (
        facts.declared_integrations
        if facts.declared_integrations is not None
        else definition.integrations
    )
    tools = []
    for name in sorted(definition.allowed_tools):
        fact = facts.tools.get(name)
        effect = fact.effect if fact else None
        tools.append(
            ToolAccess(
                name=name,
                effect=effect,
                available=fact is not None,
                capabilities=fact.capabilities if fact else frozenset(),
                denied_by_policy=effect is not None and effect in denied,
                asks_first=effect is not None and at_least(risk_of(effect), APPROVAL_THRESHOLD),
            )
        )
    return EmployeeProfile(
        id=str(definition.id),
        name=definition.name,
        title=definition.role.title,
        description=definition.role.description,
        version=definition.definition_hash[:12],
        enabled=definition.enabled,
        goals=tuple(
            goal.text for goal in sorted(definition.goals, key=lambda item: item.priority)
        ),
        capabilities=definition.capabilities,
        tools=tuple(tools),
        integrations=tuple(
            IntegrationAccess(
                name=name,
                declared=name in declared,
                connected=bool(facts.integrations.get(name)),
            )
            for name in sorted(definition.integrations | declared)
        ),
        policies=tuple(
            PolicyAccess(
                name=name,
                description=POLICY_CATALOG[name].description if name in POLICY_CATALOG else "",
                denies=denied_effects({name}),
            )
            for name in sorted(definition.policies)
        ),
        model=ModelNeeds(
            capabilities=definition.model_profile.capabilities,
            min_context_tokens=definition.model_profile.min_context_tokens,
            max_cost_per_1k_usd=definition.model_profile.max_cost_per_1k_usd,
            temperature=definition.model_profile.temperature,
        ),
        memory_scope=definition.memory_scope,
        limits=definition.limits,
        contract=definition.contract,
        readiness=readiness,
    )


def indistinguishable(definitions: list[EmployeeDefinition]) -> list[tuple[str, ...]]:
    """Groups of employees that declare exactly the same work.

    Two roles with the same capabilities, services, tools and deliverables give
    the delegator nothing to choose by but prose. Reported so a person can make
    one of them different or remove it - never resolved here.
    """
    groups: dict[tuple, list[str]] = {}
    for definition in definitions:
        key = (
            frozenset(definition.capabilities),
            frozenset(definition.integrations),
            frozenset(definition.allowed_tools),
            definition.contract.produces,
        )
        groups.setdefault(key, []).append(definition.name)
    return sorted(tuple(sorted(names)) for names in groups.values() if len(names) > 1)
