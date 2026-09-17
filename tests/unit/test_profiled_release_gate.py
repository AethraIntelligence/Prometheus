"""Phase 10's Definition of Done: changing a model cannot quietly pass the gate.

Runs record the routing profile they were measured on, and the gate reads only
the profile the machine runs now. So a cheaper catalog starts with no evidence;
a profile below the strongest model's baseline by more than allowed fails; and
cost is judged per successful result, where a cheap model that fails twice for
every pass is the expensive one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from application.validation.gate_report import gate_json, render_gate
from domain.capabilities.models import CapabilityRequirement
from domain.errors import ConfigurationError
from domain.validation.evidence import Metrics
from domain.validation.failures import FailureKind
from domain.validation.gate import GateTarget, evaluate_gate
from domain.validation.profile import RoutingProfile, profile_of
from domain.validation.run import RunStatus, ValidationRun
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.profiles import baseline_entry, current_profile
from infrastructure.llm.router import CapabilityAwareModelRouter
from infrastructure.validation.gate import load_gate

CHEAP = profile_of({"EXECUTION": "fast"})
DEAR = profile_of({"EXECUTION": "strong"})
BASELINE = replace(profile_of({"EXECUTION": "strong"}), baseline=True)


def run(passed: bool, profile: RoutingProfile | None, *, age: int, cost: float = 0.01):
    return ValidationRun.create(
        "research",
        RunStatus.PASSED if passed else RunStatus.FAILED,
        failure=FailureKind.NONE if passed else FailureKind.MODEL,
        metrics=Metrics(duration_seconds=5, cost_usd=cost),
        started_at=datetime(2026, 1, 1, tzinfo=UTC) - timedelta(minutes=age),
        profile=profile,
    )


def target(**changes) -> GateTarget:
    return GateTarget(**{"scenario": "research", "attempts": 3, "min_pass_rate": 2 / 3, **changes})


def test_runs_on_other_models_do_not_count_for_the_profile_running_now() -> None:
    history = [run(True, DEAR, age=i) for i in range(3)]

    old = evaluate_gate((target(),), history, profile=DEAR.fingerprint)
    new = evaluate_gate((target(),), history, profile=CHEAP.fingerprint)

    assert old.passed
    assert not new.passed
    violation = next(v for v in new.violations if v.metric == "attempts")
    assert violation.actual == 0
    assert CHEAP.fingerprint in violation.expected


def test_a_profile_too_far_below_the_baseline_fails() -> None:
    history = [
        *(run(True, BASELINE, age=10 + i) for i in range(3)),
        run(True, CHEAP, age=0),
        run(True, CHEAP, age=1),
        run(False, CHEAP, age=2),
    ]

    lenient = evaluate_gate(
        (target(max_baseline_drop=0.4),),
        history,
        profile=CHEAP.fingerprint,
        baseline_profile=BASELINE.fingerprint,
    )
    strict = evaluate_gate(
        (target(max_baseline_drop=0.2),),
        history,
        profile=CHEAP.fingerprint,
        baseline_profile=BASELINE.fingerprint,
    )

    assert lenient.passed
    assert lenient.results[0].baseline_pass_rate == 1.0
    assert [v.metric for v in strict.violations] == ["baseline_drop"]


def test_a_baseline_threshold_without_baseline_runs_is_not_a_pass() -> None:
    history = [run(True, CHEAP, age=i) for i in range(3)]

    report = evaluate_gate((target(max_baseline_drop=0.1),), history, profile=CHEAP.fingerprint)

    assert [v.metric for v in report.violations] == ["baseline_attempts"]


def test_baseline_evidence_from_an_old_strong_model_does_not_count() -> None:
    current_baseline = replace(profile_of({"EXECUTION": "new-strong"}), baseline=True)
    history = [
        *(run(True, BASELINE, age=i) for i in range(3)),
        *(run(True, CHEAP, age=10 + i) for i in range(3)),
    ]

    report = evaluate_gate(
        (target(max_baseline_drop=0.1),),
        history,
        profile=CHEAP.fingerprint,
        baseline_profile=current_baseline.fingerprint,
    )

    assert [violation.metric for violation in report.violations] == ["baseline_attempts"]
    assert report.results[0].baseline_attempts == 0


def test_cost_is_judged_per_successful_result() -> None:
    cheap_but_failing = [
        run(True, CHEAP, age=0, cost=0.01),
        run(False, CHEAP, age=1, cost=0.01),
        run(False, CHEAP, age=2, cost=0.01),
    ]

    report = evaluate_gate(
        (target(min_pass_rate=0, max_median_cost_usd=0.02, max_cost_per_success_usd=0.02),),
        cheap_but_failing,
        profile=CHEAP.fingerprint,
    )

    result = report.results[0]
    assert result.median_cost_usd == pytest.approx(0.01), "every call looks cheap"
    assert result.cost_per_success_usd == pytest.approx(0.03)
    assert [v.metric for v in report.violations] == ["cost_per_success_usd"]
    assert "per success" in render_gate(report)
    assert '"cost_per_success_usd": 0.03' in gate_json(report)


def test_baseline_runs_never_count_as_the_profile_they_are_compared_with() -> None:
    history = [run(True, BASELINE, age=i) for i in range(3)]

    assert not evaluate_gate((target(),), history, profile=BASELINE.fingerprint).passed
    assert not evaluate_gate((target(),), history).passed


def test_the_fingerprint_moves_with_a_default_or_with_what_an_entry_points_at() -> None:
    raw = {
        "models": {
            "fast": {"provider": "local", "model": "small", "quality": 0.4},
            "strong": {"provider": "openrouter", "model": "large", "quality": 0.9},
        },
        "defaults": {"execution": "fast"},
    }

    def fingerprint(data) -> str:
        catalog = ModelCatalog.from_dict(data)
        return current_profile(CapabilityAwareModelRouter(catalog), catalog).fingerprint

    same = fingerprint(raw)
    moved_default = fingerprint({**raw, "defaults": {"execution": "strong"}})
    swapped_model = fingerprint(
        {**raw, "models": {**raw["models"], "fast": {**raw["models"]["fast"], "model": "other"}}}
    )

    assert same == fingerprint(raw)
    assert len({same, moved_default, swapped_model}) == 3

    catalog = ModelCatalog.from_dict(raw)
    baseline = current_profile(CapabilityAwareModelRouter(catalog), catalog, baseline=True)
    assert baseline_entry(catalog).name == "strong"
    assert baseline.baseline
    assert all(route.startswith("strong ") for route in baseline.routes.values())


def test_the_fingerprint_moves_with_every_contract_field_used_for_routing() -> None:
    raw = {
        "models": {
            "fast": {
                "provider": "local",
                "model": "small",
                "latency_ms": 100,
                "input_cost_per_1k_usd": 0.001,
                "output_cost_per_1k_usd": 0.002,
            }
        }
    }

    def fingerprint(data) -> str:
        catalog = ModelCatalog.from_dict(data)
        return current_profile(CapabilityAwareModelRouter(catalog), catalog).fingerprint

    original = fingerprint(raw)
    changed_latency = fingerprint(
        {"models": {"fast": {**raw["models"]["fast"], "latency_ms": 200}}}
    )
    changed_cost = fingerprint(
        {
            "models": {
                "fast": {
                    **raw["models"]["fast"],
                    "input_cost_per_1k_usd": 0.003,
                }
            }
        }
    )

    assert len({original, changed_latency, changed_cost}) == 3


def test_a_profile_round_trips_through_the_stored_result() -> None:
    stored = run(True, CHEAP, age=0).to_result()

    assert RoutingProfile.from_dict(stored["profile"]) == CHEAP
    assert RoutingProfile.from_dict({}) is None


def test_the_release_contract_accepts_the_new_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "gate.yaml"
    path.write_text(
        "version: 1\ntargets:\n  - scenario: research\n    attempts: 3\n"
        "    min_pass_rate: 0.8\n    max_cost_per_success_usd: 0.2\n    max_baseline_drop: 0.1\n",
        encoding="utf-8",
    )
    (loaded,) = load_gate(path)
    assert (loaded.max_cost_per_success_usd, loaded.max_baseline_drop) == (0.2, 0.1)

    path.write_text(path.read_text().replace("0.1\n", "1.5\n"), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="max_baseline_drop"):
        load_gate(path)


def test_a_profile_names_the_entry_and_model_every_kind_of_work_goes_to() -> None:
    catalog = ModelCatalog.from_dict(
        {"models": {"only": {"provider": "local", "model": "m"}}}
    )

    profile = current_profile(CapabilityAwareModelRouter(catalog), catalog)

    assert profile.routes["EXECUTION"] == "only (local/m)"
    assert "EMBEDDING" not in profile.routes
    assert not CapabilityRequirement().local_only
