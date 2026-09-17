"""Strict loading of the validation release contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from domain.errors import ConfigurationError
from domain.validation.gate import GateTarget

DEFAULT_GATE_PATH = Path(__file__).resolve().parents[2] / "validation" / "release-gate.yaml"
_FIELDS = frozenset(
    {
        "scenario",
        "attempts",
        "min_pass_rate",
        "max_median_duration_seconds",
        "max_median_cost_usd",
        "max_interventions_per_run",
        "max_regression_drop",
        "safety",
        "max_cost_per_success_usd",
        "max_baseline_drop",
    }
)


def load_gate(path: Path = DEFAULT_GATE_PATH) -> tuple[GateTarget, ...]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{path}: cannot read release gate - {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"version", "targets"}:
        raise ConfigurationError(f"{path}: expected only version and targets.")
    if raw["version"] != 1:
        raise ConfigurationError(f"{path}: unsupported release gate version {raw['version']}.")
    targets = raw["targets"]
    if not isinstance(targets, list) or not targets:
        raise ConfigurationError(f"{path}: targets must be a non-empty list.")

    parsed = tuple(_target(path, index, entry) for index, entry in enumerate(targets, 1))
    names = [target.scenario for target in parsed]
    if len(names) != len(set(names)):
        raise ConfigurationError(f"{path}: each scenario may appear only once.")
    return parsed


def _target(path: Path, index: int, raw: Any) -> GateTarget:
    where = f"{path}: targets[{index}]"
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{where} must be a mapping.")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ConfigurationError(f"{where} has unknown fields: {', '.join(sorted(unknown))}.")
    scenario = str(raw.get("scenario", "")).strip()
    attempts = raw.get("attempts")
    pass_rate = raw.get("min_pass_rate")
    if not scenario:
        raise ConfigurationError(f"{where}.scenario must have text.")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 2:
        raise ConfigurationError(f"{where}.attempts must be a whole number of 2 or more.")
    _rate(where, "min_pass_rate", pass_rate)
    regression = raw.get("max_regression_drop", 0.0)
    _rate(where, "max_regression_drop", regression)

    return GateTarget(
        scenario=scenario,
        attempts=attempts,
        min_pass_rate=float(pass_rate),
        max_median_duration_seconds=_positive(where, raw, "max_median_duration_seconds"),
        max_median_cost_usd=_positive(where, raw, "max_median_cost_usd"),
        max_interventions_per_run=_positive(where, raw, "max_interventions_per_run"),
        max_regression_drop=float(regression),
        safety=bool(raw.get("safety", False)),
        max_cost_per_success_usd=_positive(where, raw, "max_cost_per_success_usd"),
        max_baseline_drop=_optional_rate(where, raw, "max_baseline_drop"),
    )


def _optional_rate(where: str, raw: dict[str, Any], field: str) -> float | None:
    value = raw.get(field)
    if value is None:
        return None
    _rate(where, field, value)
    return float(value)


def _rate(where: str, field: str, value: Any) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool) or not 0 <= value <= 1:
        raise ConfigurationError(f"{where}.{field} must be a number from 0 to 1.")


def _positive(where: str, raw: dict[str, Any], field: str) -> float | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{where}.{field} must be zero or more.")
    return float(value)

