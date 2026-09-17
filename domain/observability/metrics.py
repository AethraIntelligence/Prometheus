"""Operational metrics with explicit denominators and no-data semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil

from domain.observability.models import RunKind
from domain.workspace.models import WorkspaceId

MIN_RATE_SAMPLES = 5
MIN_P95_SAMPLES = 20


@dataclass(frozen=True, slots=True)
class RunSample:
    kind: RunKind
    workspace_id: WorkspaceId
    started_at: datetime
    terminal: bool
    succeeded: bool
    accepted: bool | None
    cancelled: bool = False
    latency_ms: int = 0
    approval_wait_ms: int = 0
    interventions: int = 0
    recoveries: int = 0
    cost_usd: float = 0.0
    unsafe_actions: int = 0
    model_profile: str = ""


@dataclass(frozen=True, slots=True)
class Metric:
    value: float | int | None
    count: int
    denominator: int
    minimum_samples: int
    unit: str

    @property
    def has_data(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    workspace_id: WorkspaceId
    window_start: datetime
    window_end: datetime
    run_kind: RunKind | None
    model_profile: str
    success_rate: Metric
    accepted_result_rate: Metric
    latency_median_ms: Metric
    latency_p95_ms: Metric
    approval_wait_median_ms: Metric
    intervention_rate: Metric
    recovery_rate: Metric
    cost_per_accepted_result_usd: Metric
    unsafe_action_count: Metric


def _rate(numerator: int, denominator: int) -> Metric:
    return Metric(
        value=(numerator / denominator if denominator >= MIN_RATE_SAMPLES else None),
        count=numerator,
        denominator=denominator,
        minimum_samples=MIN_RATE_SAMPLES,
        unit="ratio",
    )


def _percentile(values: list[int], percentile: float, minimum: int) -> Metric:
    ordered = sorted(values)
    value = None
    if len(ordered) >= minimum:
        value = ordered[max(0, ceil(percentile * len(ordered)) - 1)]
    return Metric(value, len(ordered), len(ordered), minimum, "ms")


def calculate(
    samples: list[RunSample],
    *,
    workspace_id: WorkspaceId,
    window_start: datetime,
    window_end: datetime,
    run_kind: RunKind | None = None,
    model_profile: str = "",
) -> MetricsSnapshot:
    """Calculate one scoped window; cancellation is never treated as failure."""
    selected = [
        sample
        for sample in samples
        if sample.workspace_id == workspace_id
        and window_start <= sample.started_at <= window_end
        and (run_kind is None or sample.kind is run_kind)
        and (not model_profile or sample.model_profile == model_profile)
    ]
    terminal = [sample for sample in selected if sample.terminal and not sample.cancelled]
    accepted = [sample for sample in terminal if sample.accepted is not None]
    accepted_count = sum(sample.accepted is True for sample in accepted)
    latencies = [sample.latency_ms for sample in terminal if sample.latency_ms >= 0]
    waits = [sample.approval_wait_ms for sample in selected if sample.approval_wait_ms > 0]
    total_cost = sum(sample.cost_usd for sample in accepted if sample.accepted)
    return MetricsSnapshot(
        workspace_id=workspace_id,
        window_start=window_start,
        window_end=window_end,
        run_kind=run_kind,
        model_profile=model_profile,
        success_rate=_rate(sum(sample.succeeded for sample in terminal), len(terminal)),
        accepted_result_rate=_rate(accepted_count, len(accepted)),
        latency_median_ms=_percentile(latencies, 0.5, 1),
        latency_p95_ms=_percentile(latencies, 0.95, MIN_P95_SAMPLES),
        approval_wait_median_ms=_percentile(waits, 0.5, 1),
        intervention_rate=_rate(
            sum(sample.interventions > 0 for sample in selected), len(selected)
        ),
        recovery_rate=_rate(
            sum(sample.recoveries > 0 and sample.succeeded for sample in selected),
            sum(sample.recoveries > 0 for sample in selected),
        ),
        cost_per_accepted_result_usd=Metric(
            total_cost / accepted_count if accepted_count else None,
            accepted_count,
            accepted_count,
            1,
            "USD",
        ),
        unsafe_action_count=Metric(
            sum(sample.unsafe_actions for sample in selected),
            len(selected),
            len(selected),
            0,
            "count",
        ),
    )
