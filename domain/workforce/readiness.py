"""Whether an employee can take work right now, and why not.

A role card that looks the same whether or not its integration is connected, its
sandbox exists or any model can run it is a card that lies by omission. The
person reads "Analyst", hands over a spreadsheet, and learns from a failed run
that `code.run` was never registered on this machine. So readiness is computed
here, from facts, by one pure function - and an interface renders the answer
rather than guessing one from a list of tools.

Three states, and the line between the last two is whether the role can still
do *what it declares*:

* **READY** - everything it declares is present.
* **DEGRADED** - something it lists is missing or could not be checked, but it
  can still do some of what it claims. It can work; it cannot do everything,
  and `lost_capabilities` says which work it must not be given.
* **UNAVAILABLE** - it cannot do the work it is declared for: disabled, a
  contradiction in its declaration, a required integration not connected, no
  model able to run it, a claimed capability with nothing behind it on this
  machine, or a policy that denies the effect its declared deliverable needs.

Every reason carries a machine code, a sentence, and - where one is known - what
a person could do about it. Facts nobody could gather (a router that could not
be asked) are said as such rather than read as a pass: a check that could not
run makes the role DEGRADED, never READY.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from domain.capabilities.models import Capability
from domain.employees.contract import WorkProduct
from domain.employees.definition import EmployeeDefinition
from domain.employees.validation import Issue
from domain.policies.risk import Effect
from domain.policies.rules import CATALOG as POLICY_CATALOG


class ReadinessState(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ReasonCode(StrEnum):
    DISABLED = "DISABLED"
    DEFINITION_INVALID = "DEFINITION_INVALID"
    INTEGRATION_NOT_CONNECTED = "INTEGRATION_NOT_CONNECTED"
    GRANTED_INTEGRATION_UNAVAILABLE = "GRANTED_INTEGRATION_UNAVAILABLE"
    NO_SUITABLE_MODEL = "NO_SUITABLE_MODEL"
    MODEL_CHECK_UNAVAILABLE = "MODEL_CHECK_UNAVAILABLE"
    REQUIRED_TOOL_MISSING = "REQUIRED_TOOL_MISSING"
    CAPABILITY_UNBACKED = "CAPABILITY_UNBACKED"
    POLICY_BLOCKS_DELIVERABLE = "POLICY_BLOCKS_DELIVERABLE"
    POLICY_DENIES_TOOL = "POLICY_DENIES_TOOL"


class Recovery(StrEnum):
    """Where a person goes to fix it. Named places, not URLs: each interface maps them."""

    PLUGINS = "PLUGINS"
    MODELS = "MODELS"
    GENERAL = "GENERAL"
    DECLARATION = "DECLARATION"


@dataclass(frozen=True, slots=True)
class Reason:
    code: ReasonCode
    state: ReadinessState
    message: str
    recovery: Recovery | None = None
    recovery_hint: str = ""


@dataclass(frozen=True, slots=True)
class ToolFacts:
    """What one tool on this machine does, as far as readiness cares."""

    effect: Effect
    capabilities: frozenset[Capability] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ModelFacts:
    """Whether the router could place this role's model profile.

    `available` None means the question could not be asked, which is not a yes.
    """

    available: bool | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReadinessFacts:
    #: Tools this machine offers, by name.
    tools: Mapping[str, ToolFacts]
    #: Every integration this machine has a record of, by name -> usable now.
    integrations: Mapping[str, bool]
    model: ModelFacts
    #: What `domain.employees.validation.check` found for this declaration.
    issues: tuple[Issue, ...] = ()
    #: Integrations the employee's own file names. The rest of
    #: `definition.integrations` were granted from the window and only add.
    declared_integrations: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class Readiness:
    state: ReadinessState
    reasons: tuple[Reason, ...] = ()
    #: Claimed capabilities nothing on this machine backs for this role now.
    lost_capabilities: frozenset[Capability] = field(default_factory=frozenset)

    @property
    def assignable(self) -> bool:
        return self.state is not ReadinessState.UNAVAILABLE

    def explain(self) -> str:
        blocking = [reason.message for reason in self.reasons if reason.state is self.state]
        return "; ".join(blocking) or "ready"


class WorkforceReadiness(Protocol):
    """How delegation asks whether somebody can work here right now."""

    def readiness(self, definition: EmployeeDefinition) -> Readiness: ...


#: What a deliverable needs to be allowed to do to the world.
_DELIVERABLE_EFFECTS: dict[WorkProduct, frozenset[Effect]] = {
    WorkProduct.FILE: frozenset({Effect.WRITE}),
    WorkProduct.CHANGES: frozenset({Effect.WRITE, Effect.EXECUTE, Effect.DELETE}),
}

_ORDER = {ReadinessState.READY: 0, ReadinessState.DEGRADED: 1, ReadinessState.UNAVAILABLE: 2}


def assess(definition: EmployeeDefinition, facts: ReadinessFacts) -> Readiness:
    reasons = [
        *_disabled(definition),
        *_invalid(facts.issues),
        *_integrations(definition, facts),
        *_model(facts.model),
        *_tools(definition, facts),
        *_policies(definition, facts),
    ]
    state = max(
        (reason.state for reason in reasons),
        key=lambda value: _ORDER[value],
        default=ReadinessState.READY,
    )
    ordered = sorted(reasons, key=lambda reason: (-_ORDER[reason.state], reason.code.value))
    return Readiness(
        state=state, reasons=tuple(ordered), lost_capabilities=lost_capabilities(definition, facts)
    )


def denied_effects(policies: Iterable[str]) -> frozenset[Effect]:
    """Effects a declared policy refuses outright, whatever the risk.

    Asked of the engine's own rules rather than restated: a probe request per
    effect, so a new policy is understood here the day it is added.
    """
    from domain.policies.engine import PolicyRequest
    from domain.policies.models import ActorKind, Decision, RiskLevel, SimpleActor

    probe = SimpleActor("readiness", ActorKind.EMPLOYEE, frozenset())

    denied: set[Effect] = set()
    for name in policies:
        rule = POLICY_CATALOG.get(name)
        if rule is None:
            continue
        for effect in Effect:
            decision = rule.decide(
                PolicyRequest(
                    actor=probe,
                    action="probe",
                    risk_level=RiskLevel.LOW,
                    effect=effect,
                    policies=frozenset({name}),
                )
            )
            if decision is not None and decision.decision is Decision.DENY:
                denied.add(effect)
    return frozenset(denied)


# --- The rules ----------------------------------------------------------------


def _disabled(definition: EmployeeDefinition) -> list[Reason]:
    if definition.enabled:
        return []
    return [
        Reason(
            ReasonCode.DISABLED,
            ReadinessState.UNAVAILABLE,
            "It is switched off in its declaration.",
            Recovery.DECLARATION,
            "Set enabled: true in its employee.yaml.",
        )
    ]


def _invalid(issues: tuple[Issue, ...]) -> list[Reason]:
    reasons: list[Reason] = []
    for issue in issues:
        if issue.is_error:
            reasons.append(
                Reason(
                    ReasonCode.DEFINITION_INVALID,
                    ReadinessState.UNAVAILABLE,
                    issue.message,
                    Recovery.DECLARATION,
                    "Correct its employee.yaml.",
                )
            )
    return reasons


def _integrations(definition: EmployeeDefinition, facts: ReadinessFacts) -> list[Reason]:
    declared = (
        facts.declared_integrations
        if facts.declared_integrations is not None
        else definition.integrations
    )
    reasons: list[Reason] = []
    for name in sorted(definition.integrations):
        if facts.integrations.get(name):
            continue
        known = name in facts.integrations
        what = "is connected but switched off or failing" if known else "is not connected here"
        if name in declared:
            reasons.append(
                Reason(
                    ReasonCode.INTEGRATION_NOT_CONNECTED,
                    ReadinessState.UNAVAILABLE,
                    f"Its declaration requires {name}, which {what}.",
                    Recovery.PLUGINS,
                    f"Connect or enable {name} in Settings > Plugins.",
                )
            )
        else:
            reasons.append(
                Reason(
                    ReasonCode.GRANTED_INTEGRATION_UNAVAILABLE,
                    ReadinessState.DEGRADED,
                    f"It was granted {name}, which {what}.",
                    Recovery.PLUGINS,
                    f"Reconnect {name} in Settings > Plugins.",
                )
            )
    return reasons


def _model(model: ModelFacts) -> list[Reason]:
    if model.available is True:
        return []
    if model.available is None:
        return [
            Reason(
                ReasonCode.MODEL_CHECK_UNAVAILABLE,
                ReadinessState.DEGRADED,
                "Whether a model can run it could not be checked"
                + (f": {model.detail}" if model.detail else "."),
                Recovery.MODELS,
                "Open Settings > Providers and models.",
            )
        ]
    return [
        Reason(
            ReasonCode.NO_SUITABLE_MODEL,
            ReadinessState.UNAVAILABLE,
            model.detail or "No model in the catalog can run it.",
            Recovery.MODELS,
            "Add a model with what it needs in Settings > Providers and models.",
        )
    ]


def _tools(definition: EmployeeDefinition, facts: ReadinessFacts) -> list[Reason]:
    """Missing tools degrade; losing every claimed capability disables.

    Model-backed capabilities (reasoning, vision) are the model check's to
    answer. A capability nothing held provides while nothing is missing is a
    contradiction in the declaration, already reported as DEFINITION_INVALID.
    """
    reasons: list[Reason] = []
    missing = sorted(definition.allowed_tools - set(facts.tools))
    if not missing:
        return reasons
    reasons.append(
        Reason(
            ReasonCode.REQUIRED_TOOL_MISSING,
            ReadinessState.DEGRADED,
            f"This machine does not offer {', '.join(missing)}.",
            Recovery.GENERAL,
            "Switch the feature on in Settings > General, or install what it needs.",
        )
    )
    lost = lost_capabilities(definition, facts)
    if lost:
        everything = lost >= (definition.capabilities - definition.model_profile.capabilities)
        reasons.append(
            Reason(
                ReasonCode.CAPABILITY_UNBACKED,
                ReadinessState.UNAVAILABLE if everything else ReadinessState.DEGRADED,
                f"It is declared for {', '.join(sorted(c.value for c in lost))}, and nothing "
                "it may use here provides it.",
                Recovery.GENERAL,
                "Make the missing tools available on this machine.",
            )
        )
    return reasons


def lost_capabilities(
    definition: EmployeeDefinition, facts: ReadinessFacts
) -> frozenset[Capability]:
    """Declared capabilities no tool it holds on this machine, nor its model, provides.

    Only counted while a listed tool is missing: that tool is what backed them.
    The delegator reads this to keep a degraded role away from exactly the work
    it can no longer do, and nothing else.
    """
    if not definition.allowed_tools - set(facts.tools):
        return frozenset()
    held = frozenset().union(
        frozenset(),
        *(
            facts.tools[name].capabilities
            for name in definition.allowed_tools
            if name in facts.tools
        ),
    )
    return frozenset(
        capability
        for capability in definition.capabilities
        if capability not in held and capability not in definition.model_profile.capabilities
    )


def _policies(definition: EmployeeDefinition, facts: ReadinessFacts) -> list[Reason]:
    denied = denied_effects(definition.policies)
    if not denied:
        return []
    reasons: list[Reason] = []
    blocked = sorted(
        product.value
        for product in definition.contract.produces
        if (needed := _DELIVERABLE_EFFECTS.get(product)) and needed <= denied
    )
    if blocked and definition.contract.declared:
        reasons.append(
            Reason(
                ReasonCode.POLICY_BLOCKS_DELIVERABLE,
                ReadinessState.UNAVAILABLE,
                f"It is declared to deliver {', '.join(blocked)}, and its policies "
                f"({', '.join(sorted(definition.policies))}) deny what that takes.",
                Recovery.DECLARATION,
                "Remove the policy or the deliverable from its employee.yaml.",
            )
        )
    unusable = sorted(
        name
        for name in definition.allowed_tools
        if name in facts.tools and facts.tools[name].effect in denied
    )
    if unusable:
        reasons.append(
            Reason(
                ReasonCode.POLICY_DENIES_TOOL,
                ReadinessState.DEGRADED,
                f"Its policies deny every call of {', '.join(unusable)}.",
                Recovery.DECLARATION,
                "Remove the tool or the policy from its employee.yaml.",
            )
        )
    return reasons
