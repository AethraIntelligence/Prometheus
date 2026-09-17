"""The routing profile a machine runs, and the baseline it is compared with.

A profile is not declared anywhere: it is what the router answers for each kind
of work right now, which is the only definition that cannot drift from what
actually runs. The baseline is the same question with every kind of work forced
onto the strongest text model in the catalog - the ceiling a cheaper profile is
measured against (`domain/validation/profile.py`).
"""

from __future__ import annotations

from domain.capabilities.models import CapabilityRequirement
from domain.errors import ConfigurationError
from domain.llm.models import RoutingHints, TaskKind
from domain.validation.profile import RoutingProfile, profile_of
from domain.workforce import directions
from infrastructure.llm.catalog import ModelCatalog, ModelEntry
from infrastructure.llm.router import CapabilityAwareModelRouter

#: Every kind of work a text model does. Embedding is left out: it is not
#: judged by a scenario, and its model is chosen for privacy, not pass rate.
PROFILED_KINDS = tuple(kind for kind in TaskKind if kind is not TaskKind.EMBEDDING)


def baseline_entry(catalog: ModelCatalog) -> ModelEntry:
    """The strongest text model - on quality, then the cheaper of equals."""
    text = [entry for entry in catalog.entries if entry.generates_text]
    if not text:
        raise ConfigurationError("The catalog has no model that generates text.")
    return max(
        text,
        key=lambda entry: (
            entry.quality,
            -(entry.input_cost_per_1k_usd + entry.output_cost_per_1k_usd / 4),
        ),
    )


def current_profile(
    router: CapabilityAwareModelRouter,
    catalog: ModelCatalog,
    *,
    baseline: bool = False,
) -> RoutingProfile:
    forced = baseline_entry(catalog).name if baseline else ""
    routes: dict[str, str] = {}
    with directions.given(directions.Directions(model=forced)):
        for kind in PROFILED_KINDS:
            try:
                choice = router.select(kind, CapabilityRequirement(), RoutingHints())
            except ConfigurationError:
                routes[kind.value] = "unroutable"
                continue
            routes[kind.value] = f"{choice.entry} ({choice.provider}/{choice.model})"
    models = {
        entry.name: (
            f"{entry.provider}/{entry.model} q={entry.quality} "
            f"caps={','.join(sorted(c.value for c in entry.capabilities))} "
            f"ctx={entry.context_tokens} {entry.privacy.value} "
            f"latency={entry.latency_ms} "
            f"input_cost={entry.input_cost_per_1k_usd} "
            f"output_cost={entry.output_cost_per_1k_usd}"
        )
        for entry in catalog.entries
    }
    return profile_of(routes, models=models, baseline=baseline)
