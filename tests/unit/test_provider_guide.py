"""The shipped recommendations, checked against what the platform will accept.

A recommendation is a button that adds catalog entries and routes work. One that
routes verification to a model below the judgement floor, or execution to a
model that cannot call tools, is a one-click way to a machine that fails every
task - so the file is held to the same rules the router is.
"""

from __future__ import annotations

from domain.capabilities.models import Capability
from domain.employees.verification import MIN_JUDGEMENT_QUALITY
from infrastructure.llm.guide import load_guide
from infrastructure.llm.providers import KINDS


def test_the_shipped_guide_loads_and_names_only_kinds_that_exist() -> None:
    guide = load_guide()
    kinds = {kind.name for kind in KINDS}

    assert guide.setups and guide.providers and guide.requirements
    assert {setup.kind for setup in guide.setups} <= kinds
    assert {item.kind for item in guide.providers} == kinds, "every kind gets advice"


def test_every_setup_can_do_the_work_it_routes() -> None:
    for setup in load_guide().setups:
        routed = {kind: model for model in setup.models for kind in model.route}
        if "EXECUTION" in routed:
            assert Capability.TOOL_CALLING in routed["EXECUTION"].capabilities, setup.id
        if "VERIFICATION" in routed:
            assert routed["VERIFICATION"].quality >= MIN_JUDGEMENT_QUALITY, setup.id
        names = [model.name for model in setup.models]
        assert len(names) == len(set(names)), setup.id


def test_a_setup_that_is_not_free_says_what_it_costs_and_a_free_one_costs_nothing() -> None:
    for setup in load_guide().setups:
        free = setup.badge == "Free" or setup.kind == "local"
        for model in setup.models:
            charged = model.input_cost_per_1k_usd > 0 or model.output_cost_per_1k_usd > 0
            if free:
                assert not charged, f"{setup.id}: {model.model}"
            elif "EMBEDDING" not in model.capabilities:
                assert charged, f"{setup.id}: {model.model} has no price"
