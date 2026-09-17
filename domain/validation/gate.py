"""A release decision made from recorded validation runs.

The harness records evidence; this module answers the narrower question "may
this build ship?".  It is deliberately pure so the same decision is made by the
CLI, CI and tests.  Thresholds live in a declaration outside the code because a
product target is not an implementation detail.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median

from domain.validation.failures import FailureKind
from domain.validation.run import RunStatus, ValidationRun


@dataclass(frozen=True, slots=True)
class GateTarget:
    """The minimum acceptable recent behaviour of one scenario."""

    scenario: str
    attempts: int
    min_pass_rate: float
    max_median_duration_seconds: float | None = None
    max_median_cost_usd: float | None = None
    max_interventions_per_run: float | None = None
    max_regression_drop: float = 0.0
    safety: bool = False


@dataclass(frozen=True, slots=True)
class GateViolation:
    scenario: str
    metric: str
    actual: float | int
    expected: str


@dataclass(frozen=True, slots=True)
class GateScenarioResult:
    scenario: str
    attempts: int
    passes: int
    pass_rate: float
    median_duration_seconds: float
    median_cost_usd: float
    interventions_per_run: float
    failures: tuple[tuple[FailureKind, int], ...] = ()
    violations: tuple[GateViolation, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass(frozen=True, slots=True)
class GateReport:
    results: tuple[GateScenarioResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def violations(self) -> tuple[GateViolation, ...]:
        return tuple(item for result in self.results for item in result.violations)


def evaluate_gate(targets: tuple[GateTarget, ...], runs: list[ValidationRun]) -> GateReport:
    """Evaluate the latest complete window and, when possible, its predecessor.

    Skips describe machine configuration and therefore do not consume an
    attempt.  The caller is expected to pass newest-first history, as both
    repositories do; sorting here keeps the pure API honest for other callers.
    """

    results = tuple(_evaluate(target, runs) for target in targets)
    return GateReport(results)


def _evaluate(target: GateTarget, runs: list[ValidationRun]) -> GateScenarioResult:
    attempted = sorted(
        (
            run
            for run in runs
            if run.scenario == target.scenario and run.status is not RunStatus.SKIPPED
        ),
        key=lambda run: run.started_at,
        reverse=True,
    )
    current = attempted[: target.attempts]
    previous = attempted[target.attempts : target.attempts * 2]
    count = len(current)
    passes = sum(run.passed for run in current)
    pass_rate = passes / count if count else 0.0
    duration = median([run.metrics.duration_seconds for run in current]) if current else 0.0
    cost = median([run.metrics.cost_usd for run in current]) if current else 0.0
    interventions = sum(run.metrics.interventions for run in current) / count if count else 0.0
    failures = Counter(
        run.failure
        for run in current
        if not run.passed and run.failure is not FailureKind.NONE
    )
    violations: list[GateViolation] = []

    if count < target.attempts:
        violations.append(
            GateViolation(target.scenario, "attempts", count, f">= {target.attempts}")
        )
    if pass_rate < target.min_pass_rate:
        violations.append(
            GateViolation(
                target.scenario,
                "pass_rate",
                pass_rate,
                f">= {target.min_pass_rate:.0%}",
            )
        )
    if target.safety and passes != count:
        violations.append(
            GateViolation(target.scenario, "safety_failures", count - passes, "= 0")
        )
    if (
        target.max_median_duration_seconds is not None
        and duration > target.max_median_duration_seconds
    ):
        violations.append(
            GateViolation(
                target.scenario,
                "median_duration_seconds",
                duration,
                f"<= {target.max_median_duration_seconds:g}",
            )
        )
    if target.max_median_cost_usd is not None and cost > target.max_median_cost_usd:
        violations.append(
            GateViolation(
                target.scenario,
                "median_cost_usd",
                cost,
                f"<= {target.max_median_cost_usd:g}",
            )
        )
    if (
        target.max_interventions_per_run is not None
        and interventions > target.max_interventions_per_run
    ):
        violations.append(
            GateViolation(
                target.scenario,
                "interventions_per_run",
                interventions,
                f"<= {target.max_interventions_per_run:g}",
            )
        )
    if len(previous) == target.attempts:
        previous_rate = sum(run.passed for run in previous) / len(previous)
        drop = previous_rate - pass_rate
        if drop > target.max_regression_drop:
            violations.append(
                GateViolation(
                    target.scenario,
                    "pass_rate_regression",
                    drop,
                    f"<= {target.max_regression_drop:.0%}",
                )
            )

    return GateScenarioResult(
        scenario=target.scenario,
        attempts=count,
        passes=passes,
        pass_rate=pass_rate,
        median_duration_seconds=duration,
        median_cost_usd=cost,
        interventions_per_run=interventions,
        failures=tuple(failures.most_common()),
        violations=tuple(violations),
    )

