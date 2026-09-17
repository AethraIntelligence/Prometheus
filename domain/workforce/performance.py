"""What an employee's record says about it, computed and never guessed.

Every number here has a formula, a window, a scope, a denominator and a minimum
sample, and each is written next to it because the failure it prevents is quiet:
a success rate of 100% over one run reads exactly like one over two hundred, and
a rate over "everything that ended" silently counts the work a person cancelled
as the employee's failure.

The unit is the **assignment**, not the objective. An objective is several
people's work, and charging all of its cost and its outcome to whoever touched
it last is the misattribution this module exists to avoid. The objective's own
total is shown by the Work Center, separately.

Outcomes are kept apart:

* ACCEPTED - completed, and the manager took it on the evidence;
* NOT_ACCEPTED - completed, and the evidence did not support it;
* REFUSED - the work never reached the world: no permission, or a person said no;
* FAILED - ended without a result for any other reason;
* CANCELLED - a person stopped it. Not the employee's failure, and not a
  success: excluded from the accepted-result denominator;
* OPEN - still running. Excluded from everything but the count.

**Accepted-result rate** = ACCEPTED / (ACCEPTED + NOT_ACCEPTED + REFUSED + FAILED),
over assignments made in the window. Below `MIN_SAMPLE` decided assignments it
has no value and says so. **Cost per accepted result** = the cost of every
decided assignment in the window, failures included, divided by ACCEPTED - the
price of a result, not of a call - with no value while nothing was accepted.
**Latency** is from assignment to close over decided assignments; the median
needs `MIN_SAMPLE`, p95 needs `MIN_P95_SAMPLE`. **Interventions** are approval
requests raised by its tasks, per decided assignment.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from domain.employees.contract import FailureKind
from domain.tasks.task import Task, TaskStatus
from domain.tools.refusals import REFUSED
from domain.workforce.acceptance import Acceptance, accept

#: Fewer decided assignments than this and a rate is noise.
MIN_SAMPLE = 5
#: A 95th percentile of five values is the maximum wearing a statistic's name.
MIN_P95_SAMPLE = 20
DEFAULT_WINDOW = timedelta(days=30)

_TRANSIENT = frozenset(
    {"RateLimitError", "ProviderTimeoutError", "ProviderError", "ProviderUnavailableError"}
)


class Outcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    OPEN = "OPEN"


DECIDED = frozenset({Outcome.ACCEPTED, Outcome.NOT_ACCEPTED, Outcome.REFUSED, Outcome.FAILED})


@dataclass(frozen=True, slots=True)
class AssignmentFact:
    """One assignment and what its task left behind."""

    task: Task
    assigned_at: datetime
    closed_at: datetime | None
    #: The stored verdict. None where it was never kept - it is then derived
    #: from the task record by the same rule and marked as derived.
    acceptance: Acceptance | None = None
    approvals: int = 0


@dataclass(frozen=True, slots=True)
class Classified:
    outcome: Outcome
    failure: FailureKind | None
    derived: bool


def classify(fact: AssignmentFact) -> Classified:
    task = fact.task
    if not task.is_terminal:
        return Classified(Outcome.OPEN, None, False)
    if task.status is TaskStatus.CANCELLED:
        return Classified(Outcome.CANCELLED, FailureKind.CANCELLED, False)
    if task.status is TaskStatus.COMPLETED:
        verdict = fact.acceptance
        derived = verdict is None
        if verdict is None:
            verdict = accept(task)
        if verdict.accepted:
            return Classified(Outcome.ACCEPTED, None, derived)
        if verdict.refused:
            return Classified(Outcome.REFUSED, FailureKind.REFUSED, derived)
        return Classified(Outcome.NOT_ACCEPTED, FailureKind.NOT_ACCEPTED, derived)
    return Classified(*_failed(task), False)


def _failed(task: Task) -> tuple[Outcome, FailureKind]:
    output = task.result.output if task.result else {}
    kind = task.error.kind if task.error else ""
    if kind == "PermissionDeniedError" or any(
        isinstance(item, dict) and (item.get("details") or {}).get(REFUSED)
        for item in output.get("observations") or ()
    ):
        return Outcome.REFUSED, FailureKind.REFUSED
    if output.get("stopped_by"):
        return Outcome.FAILED, FailureKind.BUDGET
    if kind in _TRANSIENT:
        return Outcome.FAILED, FailureKind.TRANSIENT
    return Outcome.FAILED, FailureKind.EXECUTION


@dataclass(frozen=True, slots=True)
class Metric:
    """A number, or the reason there is none."""

    value: float | None
    sample: int
    minimum: int
    note: str = ""

    @property
    def sufficient(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "sample": self.sample,
            "minimum": self.minimum,
            "sufficient": self.sufficient,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Performance:
    window_start: datetime
    window_end: datetime
    assignments: int
    outcomes: dict[Outcome, int]
    failures: dict[FailureKind, int]
    #: How many verdicts were derived from the record rather than stored.
    derived_verdicts: int
    accepted_rate: Metric
    cost_per_accepted_usd: Metric
    median_latency_seconds: Metric
    p95_latency_seconds: Metric
    interventions_per_assignment: Metric
    total_cost_usd: float
    scenario_pass_rate: Metric = field(
        default_factory=lambda: Metric(
            None, 0, MIN_SAMPLE, "No validation run records this employee."
        )
    )

    @property
    def has_history(self) -> bool:
        return self.assignments > 0


def measure(
    facts: Iterable[AssignmentFact],
    *,
    now: datetime,
    window: timedelta = DEFAULT_WINDOW,
    scenario_runs: Iterable[bool] = (),
) -> Performance:
    start = now - window
    inside = [fact for fact in facts if start <= fact.assigned_at <= now]
    classified = [(fact, classify(fact)) for fact in inside]
    outcomes = Counter(item.outcome for _, item in classified)
    failures = Counter(item.failure for _, item in classified if item.failure is not None)
    decided = [(fact, item) for fact, item in classified if item.outcome in DECIDED]
    accepted = outcomes[Outcome.ACCEPTED]
    cost = sum(fact.task.cost_usd for fact, _ in decided)
    durations = sorted(
        (fact.closed_at - fact.assigned_at).total_seconds()
        for fact, _ in decided
        if fact.closed_at is not None
    )
    runs = list(scenario_runs)

    return Performance(
        window_start=start,
        window_end=now,
        assignments=len(inside),
        outcomes={outcome: outcomes[outcome] for outcome in Outcome},
        failures=dict(sorted(failures.items())),
        derived_verdicts=sum(1 for _, item in classified if item.derived),
        accepted_rate=_rate(accepted, len(decided), "decided assignments"),
        cost_per_accepted_usd=(
            Metric(round(cost / accepted, 6), len(decided), 1)
            if accepted
            else Metric(None, len(decided), 1, "No accepted result in this window.")
        ),
        median_latency_seconds=_percentile(durations, 0.5, MIN_SAMPLE),
        p95_latency_seconds=_percentile(durations, 0.95, MIN_P95_SAMPLE),
        interventions_per_assignment=(
            Metric(
                round(sum(fact.approvals for fact, _ in decided) / len(decided), 3),
                len(decided),
                MIN_SAMPLE,
            )
            if len(decided) >= MIN_SAMPLE
            else _insufficient(len(decided), MIN_SAMPLE, "decided assignments")
        ),
        total_cost_usd=round(sum(fact.task.cost_usd for fact in inside), 6),
        scenario_pass_rate=(
            _rate(sum(runs), len(runs), "validation runs")
            if runs
            else Metric(None, 0, MIN_SAMPLE, "No validation run records this employee.")
        ),
    )


def _rate(numerator: int, denominator: int, unit: str, *, minimum: int = MIN_SAMPLE) -> Metric:
    if denominator < minimum:
        return _insufficient(denominator, minimum, unit)
    return Metric(round(numerator / denominator, 4), denominator, minimum)


def _insufficient(sample: int, minimum: int, unit: str) -> Metric:
    if sample == 0:
        return Metric(None, 0, minimum, f"No data: no {unit} in this window.")
    return Metric(None, sample, minimum, f"Not enough data: {sample} of {minimum} {unit}.")


def _percentile(values: list[float], fraction: float, minimum: int) -> Metric:
    if len(values) < minimum:
        return _insufficient(len(values), minimum, "finished assignments")
    # Nearest-rank: a value that actually occurred, not an interpolation of two.
    rank = max(1, math.ceil(fraction * len(values)))
    return Metric(round(values[rank - 1], 1), len(values), minimum)
