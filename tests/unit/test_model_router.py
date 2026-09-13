from __future__ import annotations

import pytest

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.errors import ConfigurationError
from domain.llm.models import RoutingHints, TaskKind
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.router import CapabilityAwareModelRouter

CATALOG = {
    "models": {
        "cheap": {
            "provider": "openrouter",
            "model": "vendor/small",
            "capabilities": ["TEXT_REASONING"],
            "context_tokens": 8000,
            "input_cost_per_1k_usd": 0.0002,
            "output_cost_per_1k_usd": 0.0008,
            "quality": 0.4,
        },
        "balanced": {
            "provider": "openrouter",
            "model": "vendor/medium",
            "capabilities": ["TEXT_REASONING", "TOOL_CALLING", "LONG_CONTEXT"],
            "context_tokens": 200000,
            "input_cost_per_1k_usd": 0.003,
            "output_cost_per_1k_usd": 0.015,
            "quality": 0.8,
        },
        "seeing": {
            "provider": "openrouter",
            "model": "vendor/vision",
            "capabilities": ["TEXT_REASONING", "TOOL_CALLING", "VISION"],
            "context_tokens": 100000,
            "input_cost_per_1k_usd": 0.01,
            "output_cost_per_1k_usd": 0.03,
            "quality": 0.9,
        },
    },
    "defaults": {"execution": "balanced", "extraction": "cheap"},
}


@pytest.fixture
def router() -> CapabilityAwareModelRouter:
    return CapabilityAwareModelRouter(ModelCatalog.from_dict(CATALOG))


def test_the_configured_default_is_used_when_it_qualifies(router) -> None:
    choice = router.select(TaskKind.EXECUTION, CapabilityRequirement(), RoutingHints())
    assert choice.model == "vendor/medium"
    assert "default" in choice.reason


def test_a_required_capability_overrules_the_default(router) -> None:
    choice = router.select(
        TaskKind.EXECUTION,
        CapabilityRequirement(required=frozenset({Capability.VISION})),
        RoutingHints(),
    )
    assert choice.model == "vendor/vision"


def test_needing_tools_is_a_requirement_not_a_preference(router) -> None:
    # Routing to a model that cannot call tools fails at run time, so the hint
    # has to bind before selection, not after.
    choice = router.select(
        TaskKind.EXTRACTION, CapabilityRequirement(), RoutingHints(needs_tools=True)
    )
    assert choice.model != "vendor/small"


def test_hints_rank_the_field_when_there_is_no_default(router) -> None:
    # PLANNING has no configured default, so the hints decide.
    frugal = router.select(
        TaskKind.PLANNING,
        CapabilityRequirement(),
        RoutingHints(quality=0.1, cost_sensitivity=1.0),
    )
    assert frugal.model == "vendor/small"

    ambitious = router.select(
        TaskKind.PLANNING,
        CapabilityRequirement(),
        RoutingHints(quality=1.0, cost_sensitivity=0.0),
    )
    assert ambitious.model == "vendor/vision"


def test_a_hint_never_overrules_the_configured_default(router) -> None:
    """Otherwise 'change the model' would mean 'find the caller that hinted'."""
    choice = router.select(
        TaskKind.EXECUTION,
        CapabilityRequirement(),
        RoutingHints(quality=1.0, cost_sensitivity=0.0),
    )
    assert choice.model == "vendor/medium"


def test_falling_back_from_an_unusable_default_says_why(router) -> None:
    # EXTRACTION defaults to 'cheap', which cannot call tools.
    choice = router.select(
        TaskKind.EXTRACTION, CapabilityRequirement(), RoutingHints(needs_tools=True)
    )
    assert choice.model != "vendor/small"
    assert "cannot do this work" in choice.reason


def test_a_context_window_that_is_too_small_is_excluded(router) -> None:
    choice = router.select(
        TaskKind.EXTRACTION, CapabilityRequirement(), RoutingHints(context_tokens=150_000)
    )
    assert choice.model == "vendor/medium"


def test_an_unsatisfiable_requirement_says_so(router) -> None:
    with pytest.raises(ConfigurationError, match="COMPUTER_USE"):
        router.select(
            TaskKind.EXECUTION,
            CapabilityRequirement(required=frozenset({Capability.COMPUTER_USE})),
            RoutingHints(),
        )


def test_changing_the_model_is_a_change_to_configuration_only() -> None:
    """The DoD for this phase, stated as a test."""
    swapped = {**CATALOG, "defaults": {"execution": "cheap"}}
    router = CapabilityAwareModelRouter(ModelCatalog.from_dict(swapped))

    choice = router.select(TaskKind.EXECUTION, CapabilityRequirement(), RoutingHints())
    assert choice.model == "vendor/small"


# --- The floor under a judgement ----------------------------------------------
#
# Phase 11 finding 3: verification handed to the cheapest entry in the catalog
# came back with the example out of its own prompt. A hint could not have fixed
# it - the configured default beats hints - so the floor is a requirement.


def test_a_quality_floor_filters_before_the_default_is_considered(router) -> None:
    choice = router.select(
        TaskKind.EXTRACTION,  # whose configured default is 'cheap'
        CapabilityRequirement(min_quality=0.5),
        RoutingHints(),
    )
    assert choice.model != "vendor/small"
    assert "cannot do this work" in choice.reason


def test_a_floor_nothing_meets_is_a_configuration_error(router) -> None:
    with pytest.raises(ConfigurationError, match="quality"):
        router.select(TaskKind.EXECUTION, CapabilityRequirement(min_quality=0.99))


def test_no_floor_leaves_the_cheapest_entry_routable(router) -> None:
    """The floor is opt-in: cheap work still goes to the cheap model."""
    choice = router.select(TaskKind.EXTRACTION, CapabilityRequirement(), RoutingHints())
    assert choice.model == "vendor/small"


def test_both_verifiers_will_not_take_the_bottom_of_a_catalog() -> None:
    """Stated where the routing is declared, so no catalog can undo it."""
    from application.employee_runtime.verifier import Verifier
    from application.prometheus.verification import ObjectiveVerifier

    for routing in (ObjectiveVerifier.routing, Verifier.routing):
        _, requirement, _ = routing()
        assert requirement.min_quality > 0.0


def test_a_model_chosen_for_the_request_beats_the_default(router) -> None:
    from domain.workforce.directions import Directions, given

    with given(Directions(model="seeing")):
        choice = router.select(TaskKind.EXECUTION, CapabilityRequirement(), RoutingHints())

    assert choice.model == "vendor/vision"
    assert choice.reason == "chosen for this request"


def test_a_chosen_model_that_cannot_do_the_work_is_passed_over(router) -> None:
    """A preference, not a requirement: the floor still filters first (ADR 0003)."""
    from domain.workforce.directions import Directions, given

    with given(Directions(model="cheap")):
        choice = router.select(
            TaskKind.EXECUTION,
            CapabilityRequirement(required=frozenset({Capability.VISION})),
            RoutingHints(),
        )

    assert choice.model == "vendor/vision"
