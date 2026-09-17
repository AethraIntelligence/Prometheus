"""One attempt at one scenario, written down so attempts can be compared.

Stored rather than printed, because the question Phase 11 exists to answer -
what works reliably, what works sometimes, what does not work at all - is a
question about a *series* of runs. A number printed to a terminal answers it for
nobody a week later.

No foreign keys, for the reason `audit_log` has none: this record outlives the
task rows it describes, and clearing history must not erase the evidence that a
capability once worked.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from domain.validation.evidence import CheckResult, CheckStatus, Evidence, Metrics
from domain.validation.failures import FailureKind
from domain.validation.profile import RoutingProfile
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class RunStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    #: The machine cannot run this one - a switch is off, a tool is absent.
    #: Neither a pass nor a failure, and mixing it into either would make the
    #: completion rate a statement about configuration.
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class ValidationRun:
    """What was asked, what came of it, what it cost, and what broke."""

    id: UUID
    scenario: str
    status: RunStatus
    failure: FailureKind = FailureKind.NONE
    summary: str = ""
    #: Written by a person after reading the run. The one field a model does
    #: not fill in, and usually the only field worth reading a month later.
    note: str = ""
    checks: tuple[CheckResult, ...] = ()
    metrics: Metrics = field(default_factory=Metrics)
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    #: The models it was measured on (Phase 10). None for a run recorded
    #: before profiles were, which no profile-scoped gate counts.
    profile: RoutingProfile | None = None
    #: The employees whose tasks the run was made of, by id. Empty for a run
    #: recorded before Phase 11, which no employee's pass rate counts.
    employee_ids: tuple[str, ...] = ()

    @classmethod
    def create(cls, scenario: str, status: RunStatus, **extra: Any) -> ValidationRun:
        return cls(id=uuid4(), scenario=scenario, status=status, **extra)

    @property
    def passed(self) -> bool:
        return self.status is RunStatus.PASSED

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.checks if not result.passed)

    def with_note(self, note: str) -> ValidationRun:
        return replace(self, note=note)

    def to_result(self) -> dict[str, Any]:
        """The stored shape, readable without importing the domain."""
        return {
            "summary": self.summary,
            "note": self.note,
            "checks": [result.to_dict() for result in self.checks],
            "metrics": self.metrics.to_dict(),
            **({"profile": self.profile.to_dict()} if self.profile else {}),
            **({"employee_ids": list(self.employee_ids)} if self.employee_ids else {}),
        }

    @staticmethod
    def checks_from(raw: list[dict[str, str]]) -> tuple[CheckResult, ...]:
        return tuple(
            CheckResult(
                check=str(entry.get("check", "")),
                status=CheckStatus(str(entry.get("status", CheckStatus.FAILED.value))),
                detail=str(entry.get("detail", "")),
            )
            for entry in raw
        )


def outcome_of(
    scenario: str,
    evidence: Evidence,
    checks: tuple[CheckResult, ...],
    failure: FailureKind,
    *,
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    started_at: datetime | None = None,
) -> ValidationRun:
    """Assemble the record. A pass is every check passing and nothing at fault."""
    passed = failure is FailureKind.NONE and all(result.passed for result in checks)
    return ValidationRun(
        id=uuid4(),
        scenario=scenario,
        status=RunStatus.PASSED if passed else RunStatus.FAILED,
        failure=FailureKind.NONE if passed else failure,
        summary=evidence.summary,
        checks=checks,
        metrics=evidence.metrics,
        workspace_id=workspace_id,
        started_at=started_at or datetime.now(UTC),
        finished_at=datetime.now(UTC),
        employee_ids=evidence.employee_ids,
    )


class ValidationRunRepository(Protocol):
    async def save(self, run: ValidationRun) -> None: ...

    async def recent(
        self,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        *,
        scenario: str | None = None,
        limit: int = 100,
    ) -> list[ValidationRun]: ...
