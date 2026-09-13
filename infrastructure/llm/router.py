"""Capability-aware routing.

A caller says what the work needs - reasoning, tools, a long context, a budget -
and gets back a model. It never says which one, which is what makes swapping
models a change to `models.toml` rather than to any employee or to Prometheus.

The precedence is deliberate and worth stating plainly:

1. **Requirements filter.** A model that cannot do the work is never a candidate.
2. **The configured default wins** whenever it survives that filter. Editing
   `models.toml` must be enough to change which model runs, so a soft hint is
   not allowed to quietly route somewhere else - and somewhere more expensive.
3. **Hints rank the rest.** They decide only among candidates when the task kind
   has no default, or the default cannot do the work.

A caller who genuinely needs something else says so as a requirement, which
filters at step 1, rather than as a preference at step 3.
"""

from __future__ import annotations

from domain.capabilities.models import CapabilityRequirement
from domain.errors import ConfigurationError
from domain.llm.models import ModelChoice, RoutingHints, TaskKind
from domain.workforce import directions
from infrastructure.llm.catalog import ModelCatalog, ModelEntry


class CapabilityAwareModelRouter:
    """Implements `domain.llm.protocols.ModelRouter`."""

    def __init__(self, catalog: ModelCatalog) -> None:
        self._catalog = catalog

    def select(
        self,
        task_kind: TaskKind,
        required: CapabilityRequirement,
        hints: RoutingHints | None = None,
    ) -> ModelChoice:
        hints = hints or RoutingHints()
        requirement = self._tighten(required, hints)
        candidates = self._catalog.candidates(requirement)

        if not candidates:
            raise ConfigurationError(
                f"No model in the catalog satisfies {sorted(requirement.required)}"
                + (
                    f" with a {requirement.min_context_tokens} token context"
                    if requirement.min_context_tokens
                    else ""
                )
                + (
                    f" at quality {requirement.min_quality} or better"
                    if requirement.min_quality
                    else ""
                )
            )

        # The person's choice for this request, above the configured default and
        # still below the requirements: it is looked for among the candidates,
        # so a model that cannot do this piece of work is passed over here and
        # the choice holds for every piece that it can.
        chosen = directions.current().model
        for entry in candidates:
            if chosen and entry.name == chosen:
                return ModelChoice(
                    provider=entry.provider,
                    model=entry.model,
                    reason="chosen for this request",
                    connection=entry.connection,
                )

        preferred = self._catalog.defaults.get(task_kind)
        for entry in candidates:
            if entry.name == preferred:
                return ModelChoice(
                    provider=entry.provider,
                    model=entry.model,
                    reason=f"configured default for {task_kind.value.lower()}",
                )

        best = max(candidates, key=lambda entry: self._score(entry, requirement, hints))
        reason = (
            f"no default for {task_kind.value.lower()}"
            if preferred is None
            else f"default '{preferred}' cannot do this work"
        )
        return ModelChoice(provider=best.provider, model=best.model, reason=reason)

    def _tighten(
        self, required: CapabilityRequirement, hints: RoutingHints
    ) -> CapabilityRequirement:
        """Fold the hints that are really requirements into the requirement.

        Asking for tool calling as a 'hint' and then getting a model that cannot
        call tools is a failure at run time, so it is treated as mandatory here.
        """
        from domain.capabilities.models import Capability

        capabilities = set(required.required)
        if hints.needs_tools:
            capabilities.add(Capability.TOOL_CALLING)

        context = required.min_context_tokens
        if hints.context_tokens is not None:
            context = max(context or 0, hints.context_tokens)

        return CapabilityRequirement(
            required=frozenset(capabilities),
            preferred=required.preferred,
            min_context_tokens=context,
            min_quality=required.min_quality,
        )

    def _score(
        self,
        entry: ModelEntry,
        requirement: CapabilityRequirement,
        hints: RoutingHints,
    ) -> tuple[float, float]:
        # A rough estimate of what a call to this model costs, on the assumption
        # that output is a fraction of input. Precise enough to rank, and it is
        # only ever used to rank.
        estimated_cost = entry.input_cost_per_1k_usd + entry.output_cost_per_1k_usd / 4

        score = entry.quality * hints.quality
        score -= estimated_cost * hints.cost_sensitivity * 10
        score += requirement.score(entry.capabilities) * 0.05
        # Cheaper breaks a tie: two models that score the same are not the same
        # bill at the end of the month.
        return (round(score, 6), -estimated_cost)
