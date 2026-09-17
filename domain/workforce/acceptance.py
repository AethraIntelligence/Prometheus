"""Prometheus accepts a result on the evidence, not on the report (§88).

The employee runtime already verifies each task against its own goal, and that
is necessary and not sufficient: the witness is the run that would report
finishing either way. This is the manager's own check, and it is deliberately
*not* a second opinion from a second model. It is a fact about what the task
did, read off the record the task left behind.

The fact it reads is narrow and was expensive to learn. A task that reached for
tools and had every one of them refused or fail has not done the work, however
its closing message reads - and the first full validation run produced exactly
that answer, an employee reporting success having touched nothing. A model
asked to notice this would sometimes notice it. A count of successful tool calls
notices it every time, costs nothing, and cannot be talked out of it.

Two things it deliberately does **not** do.

**It does not fail a task that used no tools.** Judgement is real work: deciding,
comparing, writing an answer from what was passed down. Requiring evidence of
action from work whose product is a sentence would reject the tasks the platform
is best at.

**It does not judge quality.** Whether the answer is good is the verifier's
question and the objective's criteria are where it is asked. This one answers
only whether anything happened, which is the question no amount of reading the
answer can settle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domain.employees.contract import EvidenceKind, WorkContract
from domain.tasks.task import Task, TaskStatus


class AcceptanceCode(StrEnum):
    """Why a result was or was not taken, in a word a statistic can count."""

    ACCEPTED = "ACCEPTED"
    #: Not this check's question: the task did not complete.
    NOT_COMPLETED = "NOT_COMPLETED"
    ALL_REFUSED = "ALL_REFUSED"
    ALL_FAILED = "ALL_FAILED"
    #: The role's contract names evidence the record does not show.
    EVIDENCE_MISSING = "EVIDENCE_MISSING"


@dataclass(frozen=True, slots=True)
class Acceptance:
    """Whether the manager takes this result, and what to do if not."""

    accepted: bool
    reason: str = ""
    #: True when the reason the work did not happen is that the employee was not
    #: allowed to do it. Somebody else may be, so it is worth reassigning rather
    #: than replanning - the same distinction `supervisor.classify` makes.
    refused: bool = False
    code: AcceptanceCode = AcceptanceCode.ACCEPTED

    @classmethod
    def taken(cls) -> Acceptance:
        return cls(accepted=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "refused": self.refused,
            "code": self.code.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Acceptance | None:
        """A stored verdict, or None where there is none this build can read."""
        if not isinstance(raw, dict) or not isinstance(raw.get("accepted"), bool):
            return None
        try:
            code = AcceptanceCode(str(raw.get("code", "")))
        except ValueError:
            code = (
                AcceptanceCode.ACCEPTED if raw["accepted"] else AcceptanceCode.ALL_FAILED
            )
        return cls(
            accepted=raw["accepted"],
            reason=str(raw.get("reason", "")),
            refused=bool(raw.get("refused", False)),
            code=code,
        )


def accept(task: Task, contract: WorkContract | None = None) -> Acceptance:
    """Judge one finished task on what it did, not on what it said.

    `contract` adds what this role promised to leave behind (Phase 11). It is
    the one way a task that used no tools can be refused: not because tools are
    required of everybody, but because this role declared that its work shows
    in the record, and a declaration nothing checks is prose.
    """
    if task.status is not TaskStatus.COMPLETED:
        # Not this function's question. A task that failed already says so, and
        # the supervisor decides what a failure calls for.
        return Acceptance(accepted=True, code=AcceptanceCode.NOT_COMPLETED)

    attempted = 0
    succeeded = 0
    refused = 0
    for raw in _observations(task):
        attempted += 1
        if raw.get("succeeded", True):
            succeeded += 1
        elif (raw.get("details") or {}).get("refused"):
            refused += 1

    if attempted == 0 or succeeded > 0:
        return _promised(task, contract, succeeded)

    if refused:
        return Acceptance(
            accepted=False,
            reason=(
                f"It reported success, but all {attempted} of its tool calls were "
                f"refused ({refused} without permission or approval) and none reached "
                "the world."
            ),
            refused=True,
            code=AcceptanceCode.ALL_REFUSED,
        )
    return Acceptance(
        accepted=False,
        reason=(
            f"It reported success, but all {attempted} of its tool calls failed and "
            "nothing it tried to do actually happened."
        ),
        code=AcceptanceCode.ALL_FAILED,
    )


def written(task: Task) -> tuple[str, ...]:
    """Files the record shows this task writing, in order, without repeats."""
    paths: list[str] = []
    for raw in _observations(task):
        path = (raw.get("details") or {}).get("wrote")
        if raw.get("succeeded", True) and isinstance(path, str) and path not in paths:
            paths.append(path)
    return tuple(paths)


def _promised(task: Task, contract: WorkContract | None, succeeded: int) -> Acceptance:
    if contract is None:
        return Acceptance.taken()
    if EvidenceKind.TOOL_RESULT in contract.evidence and succeeded == 0:
        return Acceptance(
            accepted=False,
            reason=(
                "It reported success, but its role promises a result reached through "
                "a tool and no tool call succeeded."
            ),
            code=AcceptanceCode.EVIDENCE_MISSING,
        )
    if EvidenceKind.ARTIFACT in contract.evidence and not written(task):
        return Acceptance(
            accepted=False,
            reason=(
                "It reported success, but its role promises a file and the record "
                "shows none written."
            ),
            code=AcceptanceCode.EVIDENCE_MISSING,
        )
    return Acceptance.taken()


def _observations(task: Task) -> tuple[dict, ...]:
    raw = (task.result.output.get("observations") if task.result else None) or ()
    return tuple(item for item in raw if isinstance(item, dict))
