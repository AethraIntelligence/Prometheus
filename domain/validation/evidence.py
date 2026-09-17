"""What one run left behind, and whether it is what was asked for.

Gathered by whoever ran the scenario and then read by pure functions, for the
reason the policy engine is a pure function: a verdict that depends on a clock,
a file handle or a live database decides differently on Tuesday, and a record of
what the platform can do is worth nothing if it is not comparable across runs.

The counters are not a second accounting system. Every one of them is read back
out of what the platform already writes down - the task rows, `tool_calls`, the
audit log - and assembling them here is what turns three stores into one line a
person can compare with last week's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from domain.validation.scenario import Expectations


@dataclass(frozen=True, slots=True)
class Metrics:
    """The four numbers Phase 11 asks for (§11.4), plus what explains them.

    Interventions is deliberately the count of times a *person* had to answer,
    not the count of times one was asked: an unattended run that is refused by
    default was not an intervention, and counting it as one would say the
    platform needs a human where in fact it needs a policy.
    """

    steps: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    tasks: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    denied_actions: int = 0
    approvals_requested: int = 0
    interventions: int = 0
    recalled: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "steps": self.steps,
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "tasks": self.tasks,
            "tool_calls": self.tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "denied_actions": self.denied_actions,
            "approvals_requested": self.approvals_requested,
            "interventions": self.interventions,
            "recalled": self.recalled,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> Metrics:
        def number(key: str) -> float:
            value = raw.get(key, 0)
            return float(value) if isinstance(value, int | float) else 0.0

        return cls(
            steps=int(number("steps")),
            cost_usd=number("cost_usd"),
            duration_seconds=number("duration_seconds"),
            tasks=int(number("tasks")),
            tool_calls=int(number("tool_calls")),
            failed_tool_calls=int(number("failed_tool_calls")),
            denied_actions=int(number("denied_actions")),
            approvals_requested=int(number("approvals_requested")),
            interventions=int(number("interventions")),
            recalled=int(number("recalled")),
        )


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything a verdict is allowed to depend on."""

    succeeded: bool = False
    summary: str = ""
    #: What the run itself said was still missing - the objective's own verdict,
    #: which is a different judgement from the scenario's and worth keeping both.
    missing: tuple[str, ...] = ()
    metrics: Metrics = field(default_factory=Metrics)
    #: Tools that ran and returned. Names only: arguments are in `tool_calls`,
    #: already redacted, and a scenario has no business asserting on them.
    tools_used: tuple[str, ...] = ()
    tools_failed: tuple[str, ...] = ()
    tools_denied: tuple[str, ...] = ()
    #: Workspace-relative paths the runner was asked about and found.
    files_present: tuple[str, ...] = ()
    #: Named files that exist and were not modified by the run.
    files_untouched: tuple[str, ...] = ()
    #: The text of those files, for the checks that look inside one.
    file_text: dict[str, str] = field(default_factory=dict)
    #: The exception type that ended the run, if one did. A type, never a
    #: message: the platform classifies failures by type everywhere else.
    #: How many different employees the run's tasks were assigned to. A count
    #: rather than names: the question a scenario asks is whether the work was
    #: shared, and the names are on the task rows for anybody reading one run.
    employees: int = 0
    #: Which employees, by id (Phase 11). Ids rather than names because an id is
    #: what a task row holds and it survives a rename of nothing; a profile
    #: matches its own id to count the scenario runs its work was part of.
    employee_ids: tuple[str, ...] = ()
    #: The same employees by name, where the harness could resolve them.
    employee_names: tuple[str, ...] = ()
    #: Tasks whose stored verdict was "not accepted", by goal; and how many
    #: verdicts there were at all, so "none refused" is not read off "none judged".
    unaccepted: tuple[str, ...] = ()
    judged: int = 0
    error_type: str = ""
    error_message: str = ""


class CheckStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One expectation, and what the run did about it."""

    check: str
    status: CheckStatus
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status is CheckStatus.PASSED

    def to_dict(self) -> dict[str, str]:
        return {"check": self.check, "status": self.status.value, "detail": self.detail}


def _result(name: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASSED if ok else CheckStatus.FAILED, detail)


def check(expect: Expectations, evidence: Evidence) -> tuple[CheckResult, ...]:
    """Every declared expectation against the evidence, in declaration order.

    All of them are evaluated, even after one has failed. A run that missed
    three things and a run that missed one are different situations, and
    stopping at the first would report them identically.
    """
    results: list[CheckResult] = []

    if expect.must_succeed:
        results.append(
            _result(
                "finished",
                evidence.succeeded,
                "" if evidence.succeeded else (evidence.error_message or "did not finish"),
            )
        )

    lowered = evidence.summary.lower()
    for phrase in expect.output_contains:
        results.append(
            _result(
                f"output contains '{phrase}'",
                phrase.lower() in lowered,
                "" if phrase.lower() in lowered else "not in the answer",
            )
        )

    for phrase in expect.output_excludes:
        # A fact that exists only in another workspace's documents appearing
        # here is the failure; the answer saying it does not know is the pass.
        results.append(
            _result(
                f"output does not contain '{phrase}'",
                phrase.lower() not in lowered,
                "" if phrase.lower() not in lowered else "it is in the answer",
            )
        )

    present = set(evidence.files_present)
    for path in expect.files_exist:
        results.append(
            _result(f"file '{path}'", path in present, "" if path in present else "not written")
        )

    for path, text in expect.file_contains.items():
        content = evidence.file_text.get(path)
        if content is None:
            results.append(_result(f"'{path}' contains '{text}'", False, "file not written"))
            continue
        found = text.lower() in content.lower()
        results.append(
            _result(f"'{path}' contains '{text}'", found, "" if found else "not in the file")
        )

    untouched = set(evidence.files_untouched)
    for path in expect.files_unchanged:
        results.append(
            _result(
                f"'{path}' unchanged",
                path in untouched,
                "" if path in untouched else "changed or missing",
            )
        )

    denied = set(evidence.tools_denied)
    for tool in expect.tools_denied:
        results.append(
            _result(
                f"{tool} refused",
                tool in denied,
                "" if tool in denied else "was never refused",
            )
        )

    used = set(evidence.tools_used)
    for tool in expect.tools_forbidden:
        results.append(
            _result(f"{tool} never ran", tool not in used, "" if tool not in used else "it ran")
        )

    if expect.min_employees is not None:
        involved = evidence.employees
        results.append(
            _result(
                f"at least {expect.min_employees} employee(s)",
                involved >= expect.min_employees,
                "" if involved >= expect.min_employees else f"the work reached {involved}",
            )
        )

    involved_names = set(evidence.employee_names)
    for name in expect.employees_involved:
        results.append(
            _result(
                f"{name} took part",
                name in involved_names,
                ""
                if name in involved_names
                else f"the work went to {', '.join(sorted(involved_names)) or 'nobody'}",
            )
        )

    if expect.results_accepted:
        expected = evidence.metrics.tasks
        accepted = expected > 0 and evidence.judged == expected and not evidence.unaccepted
        results.append(
            _result(
                "every result accepted on the evidence",
                accepted,
                ""
                if accepted
                else f"only {evidence.judged} of {expected} result(s) were judged"
                if evidence.judged != expected
                else f"not accepted: {'; '.join(evidence.unaccepted)}",
            )
        )

    if expect.max_cost_usd is not None:
        spent = evidence.metrics.cost_usd
        results.append(
            _result(
                f"cost under ${expect.max_cost_usd:.4f}",
                spent <= expect.max_cost_usd,
                f"spent ${spent:.6f}",
            )
        )

    if expect.max_steps is not None:
        steps = evidence.metrics.steps
        results.append(
            _result(
                f"at most {expect.max_steps} step(s)",
                steps <= expect.max_steps,
                f"took {steps}",
            )
        )

    return tuple(results)
