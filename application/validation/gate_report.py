"""Human and machine views of one release-gate decision."""

from __future__ import annotations

import json
from typing import Any

from domain.validation.gate import GateReport


def render_gate(report: GateReport) -> str:
    lines = [
        "# Validation release gate",
        "",
        "PASS" if report.passed else "FAIL",
        "",
    ]
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        lines.append(
            f"- [{mark}] `{result.scenario}` — {result.passes}/{result.attempts} passed, "
            f"median {result.median_duration_seconds:.1f}s, "
            f"${result.median_cost_usd:.4f}, "
            f"{result.interventions_per_run:.2f} intervention(s)/run"
        )
        for violation in result.violations:
            actual = (
                f"{violation.actual:.1%}"
                if violation.metric in {"pass_rate", "pass_rate_regression"}
                else str(violation.actual)
            )
            lines.append(
                f"  - {violation.metric}: {actual}; expected {violation.expected}"
            )
        if result.failures:
            kinds = ", ".join(f"{kind.value} x{count}" for kind, count in result.failures)
            lines.append(f"  - failures: {kinds}")
    lines.append("")
    return "\n".join(lines)


def gate_json(report: GateReport) -> str:
    return json.dumps(_as_dict(report), indent=2, sort_keys=True)


def _as_dict(report: GateReport) -> dict[str, Any]:
    return {
        "passed": report.passed,
        "scenarios": [
            {
                "scenario": result.scenario,
                "passed": result.passed,
                "attempts": result.attempts,
                "passes": result.passes,
                "pass_rate": result.pass_rate,
                "median_duration_seconds": result.median_duration_seconds,
                "median_cost_usd": result.median_cost_usd,
                "interventions_per_run": result.interventions_per_run,
                "failures": {kind.value: count for kind, count in result.failures},
                "violations": [
                    {
                        "metric": violation.metric,
                        "actual": violation.actual,
                        "expected": violation.expected,
                    }
                    for violation in result.violations
                ],
            }
            for result in report.results
        ],
    }

