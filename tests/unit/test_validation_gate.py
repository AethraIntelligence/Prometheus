"""The release decision is strict, reproducible and provider-free."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from application.validation.gate_report import gate_json, render_gate
from domain.errors import ConfigurationError
from domain.validation.evidence import Metrics
from domain.validation.failures import FailureKind
from domain.validation.gate import GateTarget, evaluate_gate
from domain.validation.run import RunStatus, ValidationRun
from infrastructure.validation.gate import load_gate


def run(
    status: RunStatus,
    *,
    age: int,
    scenario: str = "research",
    duration: float = 10,
    cost: float = 0.01,
    interventions: int = 0,
    failure: FailureKind = FailureKind.NONE,
) -> ValidationRun:
    return ValidationRun.create(
        scenario,
        status,
        failure=failure,
        metrics=Metrics(
            duration_seconds=duration,
            cost_usd=cost,
            interventions=interventions,
        ),
        started_at=datetime(2026, 1, 1, tzinfo=UTC) - timedelta(minutes=age),
    )


def target(**changes: object) -> GateTarget:
    values = {
        "scenario": "research",
        "attempts": 3,
        "min_pass_rate": 2 / 3,
        "max_median_duration_seconds": 20,
        "max_median_cost_usd": 0.02,
        "max_interventions_per_run": 0,
        "max_regression_drop": 1 / 3,
    }
    return GateTarget(**{**values, **changes})  # type: ignore[arg-type]


def test_the_latest_complete_window_meets_every_threshold() -> None:
    report = evaluate_gate(
        (target(),),
        [
            run(RunStatus.PASSED, age=0),
            run(RunStatus.FAILED, age=1, failure=FailureKind.MODEL),
            run(RunStatus.PASSED, age=2),
            run(RunStatus.FAILED, age=3, failure=FailureKind.TOOL),
        ],
    )

    result = report.results[0]
    assert report.passed
    assert result.attempts == 3
    assert result.pass_rate == pytest.approx(2 / 3)
    assert result.failures == ((FailureKind.MODEL, 1),)


def test_skips_do_not_hide_a_missing_attempt() -> None:
    report = evaluate_gate(
        (target(),),
        [run(RunStatus.PASSED, age=0), run(RunStatus.SKIPPED, age=1)],
    )

    assert not report.passed
    assert [(item.metric, item.actual) for item in report.violations] == [
        ("attempts", 1),
    ]


def test_a_drop_from_the_previous_window_is_a_regression() -> None:
    history = [
        run(RunStatus.PASSED, age=0),
        run(RunStatus.FAILED, age=1, failure=FailureKind.MODEL),
        run(RunStatus.FAILED, age=2, failure=FailureKind.MODEL),
        run(RunStatus.PASSED, age=3),
        run(RunStatus.PASSED, age=4),
        run(RunStatus.PASSED, age=5),
    ]

    report = evaluate_gate(
        (target(min_pass_rate=0, max_regression_drop=0.2),),
        history,
    )

    assert not report.passed
    assert report.violations[0].metric == "pass_rate_regression"
    assert report.violations[0].actual == pytest.approx(2 / 3)


def test_a_safety_target_allows_no_failed_attempt() -> None:
    report = evaluate_gate(
        (target(min_pass_rate=0, safety=True),),
        [
            run(RunStatus.PASSED, age=0),
            run(RunStatus.PASSED, age=1),
            run(RunStatus.FAILED, age=2, failure=FailureKind.EXPECTATION),
        ],
    )

    assert {item.metric for item in report.violations} == {"safety_failures"}


def test_the_machine_report_contains_the_same_decision_as_the_human_one() -> None:
    report = evaluate_gate((target(),), [run(RunStatus.PASSED, age=0)])

    assert "FAIL" in render_gate(report)
    assert json.loads(gate_json(report))["passed"] is False
    assert json.loads(gate_json(report))["scenarios"][0]["scenario"] == "research"


def test_the_shipped_release_gate_names_real_scenarios() -> None:
    from infrastructure.validation.yaml_registry import YamlScenarioRegistry

    targets = load_gate()
    declared = {scenario.name for scenario in YamlScenarioRegistry().list_all()}

    assert len([target for target in targets if not target.safety]) == 6
    assert sum(target.safety for target in targets) == 1
    assert {target.scenario for target in targets} <= declared


@pytest.mark.parametrize(
    "body",
    [
        "version: 2\ntargets: []",
        "version: 1\ntargets: []",
        "version: 1\ntargets: [{scenario: x, attempts: 1, min_pass_rate: 1}]",
        "version: 1\ntargets: [{scenario: x, attempts: 2, min_pass_rate: 2}]",
        "version: 1\ntargets: [{scenario: x, attempts: 2, min_pass_rate: 1, typo: 4}]",
    ],
)
def test_a_release_contract_that_cannot_measure_is_refused(tmp_path: Path, body: str) -> None:
    path = tmp_path / "gate.yaml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_gate(path)

