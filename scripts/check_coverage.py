"""Coverage floors for the code whose failure costs the most, each measured on its own.

A single percentage for the whole platform averages the parts that decide
whether an action needs a person with the parts that draw a list, and a drop in
the first hides inside the second. So the policy is per area, and each floor is
set just under what the area has, so it can only be held or raised:

    uv run pytest --cov=app --cov=application --cov=domain --cov=infrastructure \\
        --cov-report=json:coverage.json
    uv run python scripts/check_coverage.py coverage.json

Migration scripts under `migrations/versions/` are excluded: each runs once per
installation and is exercised by the upgrade tests, not line by line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Area -> (path prefixes, minimum percent of lines covered).
FLOORS: dict[str, tuple[tuple[str, ...], float]] = {
    "policy": (("domain/policies/",), 95.0),
    "approval": (
        (
            "domain/approvals/",
            "application/employee_runtime/approvals.py",
            "infrastructure/approvals/",
        ),
        90.0,
    ),
    "migration and backup": (
        (
            "infrastructure/runtime/migration.py",
            "infrastructure/runtime/backup.py",
            "domain/safety/schema.py",
            "domain/safety/backup.py",
        ),
        82.0,
    ),
    "persistence": (("infrastructure/persistence/",), 86.0),
    "routing": (
        (
            "infrastructure/llm/router.py",
            "infrastructure/llm/directed.py",
            "domain/llm/routing.py",
            "domain/llm/escalation.py",
        ),
        95.0,
    ),
    "parsers": (
        (
            "domain/safety/emergency.py",
            "domain/integrations/provenance.py",
            "infrastructure/integrations/yaml_catalog.py",
            "infrastructure/workflows/yaml_registry.py",
            "infrastructure/employees/yaml_registry.py",
            "domain/scheduling/models.py",
        ),
        85.0,
    ),
    "emergency stop and ownership": (
        (
            "domain/safety/",
            "application/safety/",
            "infrastructure/runtime/lock.py",
            "infrastructure/computer/stop.py",
            "infrastructure/tasks/effects.py",
        ),
        86.0,
    ),
}


def main() -> int:
    report = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json").read_text())
    failed = False
    for area, (prefixes, floor) in FLOORS.items():
        covered = total = 0
        for name, data in report["files"].items():
            path = Path(name).as_posix()
            if "/migrations/versions/" in path or not path.startswith(prefixes):
                continue
            covered += data["summary"]["covered_lines"]
            total += data["summary"]["num_statements"]
        if total == 0:
            print(f"{area}: no lines measured - the prefixes no longer match anything")
            failed = True
            continue
        percent = 100.0 * covered / total
        status = "ok" if percent >= floor else "BELOW FLOOR"
        failed = failed or percent < floor
        print(f"{area}: {percent:.1f}% of {total} lines (floor {floor:.0f}%) {status}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
