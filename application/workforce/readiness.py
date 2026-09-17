"""Gathering the facts readiness is decided from, at the moment it is asked.

`domain.workforce.readiness.assess` decides; this only looks. It asks the tool
registry what exists, the integration snapshot what is connected, and the router
whether each stage of a run could be placed - every time, with no cache, because
connecting a service or saving a model in Settings must change the answer on the
next read without a restart, and a cached "unavailable" would keep the delegator
refusing somebody who has just become able.

Each source is handed in as a way to get it, not as the thing itself: the router
and the registry are replaced when a person changes a setting, and a service
holding the old one would answer about a machine that no longer exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import structlog

from domain.capabilities.models import CapabilityRequirement
from domain.employees.definition import EmployeeDefinition
from domain.employees.validation import check
from domain.errors import PrometheusError
from domain.integrations.models import Integration
from domain.llm.models import RoutingHints, TaskKind
from domain.llm.protocols import ModelRouter
from domain.policies.models import ActorKind, SimpleActor
from domain.tools.protocols import ToolRegistry
from domain.workforce.readiness import (
    ModelFacts,
    Readiness,
    ReadinessFacts,
    ToolFacts,
    assess,
)

log = structlog.get_logger(__name__)

#: What one stage of a run asks the router for.
Stage = tuple[TaskKind, CapabilityRequirement, RoutingHints]

#: The machine's view, asked as a person looking at it rather than as any one
#: employee - which would filter the answer by that employee's own grants.
_EVERYTHING = SimpleActor("readiness", ActorKind.USER, frozenset({"*"}))


class ReadinessService:
    """Implements `domain.workforce.readiness.WorkforceReadiness`."""

    def __init__(
        self,
        *,
        tools: Callable[[], ToolRegistry | None],
        integrations: Callable[[], Iterable[Integration]] = lambda: (),
        router: Callable[[], ModelRouter | None] = lambda: None,
        stages: Callable[[EmployeeDefinition], Iterable[Stage]] = lambda _: (),
        declared_integrations: Callable[[str], frozenset[str] | None] = lambda _: None,
    ) -> None:
        self._tools = tools
        self._integrations = integrations
        self._router = router
        self._stages = stages
        self._declared = declared_integrations

    def facts(self, definition: EmployeeDefinition) -> ReadinessFacts:
        offered = self._offered()
        integrations = {item.name: item.is_usable for item in self._integrations()}
        return ReadinessFacts(
            tools=offered,
            integrations=integrations,
            model=self._model(definition),
            issues=check(
                definition,
                {name: fact.capabilities for name, fact in offered.items()},
                integrations,
            ),
            declared_integrations=self._declared(definition.name),
        )

    def readiness(self, definition: EmployeeDefinition) -> Readiness:
        return assess(definition, self.facts(definition))

    def _offered(self) -> dict[str, ToolFacts]:
        registry = self._tools()
        if registry is None:
            return {}
        return {
            spec.name: ToolFacts(effect=spec.effect, capabilities=spec.capabilities)
            for spec in registry.list_specs(_EVERYTHING)
        }

    def _model(self, definition: EmployeeDefinition) -> ModelFacts:
        stages = list(self._stages(definition))
        if not stages:
            return ModelFacts(available=True)
        try:
            router = self._router()
        except Exception as error:  # a catalog that cannot be read is not a yes
            return ModelFacts(available=None, detail=str(error))
        if router is None:
            return ModelFacts(available=None, detail="no model router is configured")
        for kind, requirement, hints in stages:
            try:
                router.select(kind, requirement, hints)
            except PrometheusError as error:
                return ModelFacts(
                    available=False, detail=f"{kind.value.lower().capitalize()}: {error}"
                )
            except Exception as error:
                log.warning(
                    "readiness.model_check_failed", employee=definition.name, error=str(error)
                )
                return ModelFacts(available=None, detail=str(error))
        return ModelFacts(available=True)
