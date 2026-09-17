"""A release decision made from recorded validation runs.

The harness records evidence; this module answers the narrower question "may
this build ship?".

Since Phase 10 it answers it *for the models the machine routes to now*. Given a
profile fingerprint, only runs measured under that profile count, so a catalog
change cannot keep borrowing the pass rate of the models it replaced - it starts
with no attempts and fails `attempts` until it has earned them. Runs forced onto
the strongest model are the baseline: a profile may be cheaper, and may not fall
further below the baseline than the target allows. And cost is judged per
successful result, because a cheap call that fails and is repeated is not
cheap.  It is deliberately pure so the same decision is made by the
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
    #: Total cost of the window divided by its passes. What a result costs,
    #: failures included - the saving Phase 10 is measured in.
    max_cost_per_success_usd: float | None = None
    #: How far below the baseline's pass rate this profile may be. None means
    #: the baseline is reported and not enforced.
    max_baseline_drop: float | None = None


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
    #: None when nothing passed: no successful result has a price.
    cost_per_success_usd: float | None = None
    baseline_attempts: int = 0
    baseline_pass_rate: float | None = None
    failures: tuple[tuple[FailureKind, int], ...] = ()
    violations: tuple[GateViolation, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass(frozen=True, slots=True)
class GateReport:
    results: tuple[GateScenarioResult, ...]
    #: The profile the decision was made for. Empty: every run counted, as
    #: before profiles were recorded.
    profile: str = ""

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def violations(self) -> tuple[GateViolation, ...]:
        return tuple(item for result in self.results for item in result.violations)


def evaluate_gate(
    targets: tuple[GateTarget, ...],
    runs: list[ValidationRun],
    *,
    profile: str = "",
    baseline_profile: str = "",
) -> GateReport:
    """Evaluate the latest complete window and, when possible, its predecessor.

    Skips describe machine configuration and therefore do not consume an
    attempt.  The caller is expected to pass newest-first history, as both
    repositories do; sorting here keeps the pure API honest for other callers.
    """

    baseline = [
        run
        for run in runs
        if run.profile is not None
        and run.profile.baseline
        and (not baseline_profile or run.profile.fingerprint == baseline_profile)
    ]
    if profile:
        measured = [
            run
            for run in runs
            if run.profile is not None
            and not run.profile.baseline
            and run.profile.fingerprint == profile
        ]
    else:
        measured = [run for run in runs if run.profile is None or not run.profile.baseline]
    results = tuple(_evaluate(target, measured, baseline, profile) for target in targets)
    return GateReport(results, profile=profile)


def _attempted(target: GateTarget, runs: list[ValidationRun]) -> list[ValidationRun]:
    return sorted(
        (
            run
            for run in runs
            if run.scenario == target.scenario and run.status is not RunStatus.SKIPPED
        ),
        key=lambda run: run.started_at,
        reverse=True,
    )


def _evaluate(
    target: GateTarget,
    runs: list[ValidationRun],
    baseline_runs: list[ValidationRun],
    profile: str,
) -> GateScenarioResult:
    attempted = _attempted(target, runs)
    current = attempted[: target.attempts]
    previous = attempted[target.attempts : target.attempts * 2]
    count = len(current)
    passes = sum(run.passed for run in current)
    pass_rate = passes / count if count else 0.0
    duration = median([run.metrics.duration_seconds for run in current]) if current else 0.0
    cost = median([run.metrics.cost_usd for run in current]) if current else 0.0
    interventions = sum(run.metrics.interventions for run in current) / count if count else 0.0
    total_cost = sum(run.metrics.cost_usd for run in current)
    per_success = total_cost / passes if passes else None
    baseline = _attempted(target, baseline_runs)[: target.attempts]
    baseline_rate = (
        sum(run.passed for run in baseline) / len(baseline) if baseline else None
    )
    failures = Counter(
        run.failure
        for run in current
        if not run.passed and run.failure is not FailureKind.NONE
    )
    violations: list[GateViolation] = []

    if count < target.attempts:
        violations.append(
            GateViolation(
                target.scenario,
                "attempts",
                count,
                f">= {target.attempts}" + (f" under profile {profile}" if profile else ""),
            )
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
    if (
        target.max_cost_per_success_usd is not None
        and count
        and (per_success is None or per_success > target.max_cost_per_success_usd)
    ):
        violations.append(
            GateViolation(
                target.scenario,
                "cost_per_success_usd",
                per_success if per_success is not None else float("inf"),
                f"<= {target.max_cost_per_success_usd:g}",
            )
        )
    if target.max_baseline_drop is not None:
        if len(baseline) < target.attempts:
            violations.append(
                GateViolation(
                    target.scenario,
                    "baseline_attempts",
                    len(baseline),
                    f">= {target.attempts} on the strongest model",
                )
            )
        elif baseline_rate is not None and baseline_rate - pass_rate > target.max_baseline_drop:
            violations.append(
                GateViolation(
                    target.scenario,
                    "baseline_drop",
                    baseline_rate - pass_rate,
                    f"<= {target.max_baseline_drop:.0%}",
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
        cost_per_success_usd=per_success,
        baseline_attempts=len(baseline),
        baseline_pass_rate=baseline_rate,
        failures=tuple(failures.most_common()),
        violations=tuple(violations),
    )
