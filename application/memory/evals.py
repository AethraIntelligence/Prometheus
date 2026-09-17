"""Retrieval evals: does recall return what it should, and only that?

Unit tests say the rules are written correctly. These say the *backend* obeys
them on a realistic mix - several workspaces, private notes, plans, corrections,
expired notes - and they are run against whichever `Memory` is handed in, so the
same cases measure the in-memory store, SQLite and PostgreSQL alike
(`prometheus memory-eval`, `tests/integration/test_retrieval_evals.py`).

Three properties, each with a threshold stated before anything is run:

* **groundedness** - everything recalled can be traced to a source, and answers
  the question rather than filling the budget; what should be found is found.
* **freshness** - a superseded or expired memory is never recalled, and between
  two live answers to one question the recent one comes first.
* **isolation** - nothing crosses a workspace, a plan or another employee's
  private notes. The threshold is 1.0 and is not a tuning knob: one leak is the
  failure, not a lower score.

The corpus is fixed and synthetic on purpose. A score that moved because the
data moved would measure the data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryStatus,
    Provenance,
    SourceKind,
)
from domain.memory.protocols import Memory
from domain.workspace.models import WorkspaceId

GROUNDEDNESS = "groundedness"
FRESHNESS = "freshness"
ISOLATION = "isolation"

THRESHOLDS: dict[str, float] = {GROUNDEDNESS: 1.0, FRESHNESS: 1.0, ISOLATION: 1.0}

#: The moment every case is asked as of, so decay and expiry never depend on
#: when the eval happens to run.
AS_OF = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

HOME = WorkspaceId("eval-home")
AWAY = WorkspaceId("eval-away")
ANALYST = UUID("00000000-0000-4000-8000-00000000a001")
WRITER = UUID("00000000-0000-4000-8000-00000000a002")
PLAN = UUID("00000000-0000-4000-8000-00000000b001")
OTHER_PLAN = UUID("00000000-0000-4000-8000-00000000b002")


@dataclass(frozen=True, slots=True)
class EvalCase:
    name: str
    metric: str
    query: MemoryQuery
    #: Keys that must be returned.
    expected: frozenset[str] = frozenset()
    #: Keys that must not be returned.
    forbidden: frozenset[str] = frozenset()
    #: Pairs (earlier, later) that must come back in this order.
    ordered: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: EvalCase
    returned: tuple[str, ...]
    problems: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    backend: str
    results: tuple[CaseResult, ...] = ()
    thresholds: dict[str, float] = field(default_factory=lambda: dict(THRESHOLDS))

    @property
    def scores(self) -> dict[str, float]:
        scores: dict[str, float] = {}
        for metric in self.thresholds:
            chosen = [result for result in self.results if result.case.metric == metric]
            scores[metric] = (
                sum(result.passed for result in chosen) / len(chosen) if chosen else 1.0
            )
        return scores

    @property
    def passed(self) -> bool:
        scores = self.scores
        return all(scores[metric] >= floor for metric, floor in self.thresholds.items())


def corpus() -> dict[str, MemoryItem]:
    """The fixed memories every eval run starts from, by key."""
    day = timedelta(days=1)

    def item(key: str, content: str, **extra) -> MemoryItem:
        extra.setdefault("scope", MemoryScope.WORKSPACE)
        extra.setdefault("kind", MemoryKind.SEMANTIC)
        extra.setdefault("workspace_id", HOME)
        extra.setdefault("basis", MemoryBasis.STATED)
        extra.setdefault("confidence", 0.9)
        extra.setdefault(
            "provenance", Provenance(kind=SourceKind.PERSON, ref=key, label=f"eval {key}")
        )
        extra.setdefault("created_at", AS_OF - 2 * day)
        return MemoryItem.create(
            content,
            scope=extra.pop("scope"),
            kind=extra.pop("kind"),
            workspace_id=extra.pop("workspace_id"),
            metadata={"eval": key},
            **extra,
        )

    current = item("report_current", "The quarterly report lives in reports/q3.md")
    items = [
        current,
        item(
            "report_superseded",
            "The quarterly report lives in reports/q2.md",
            status=MemoryStatus.SUPERSEDED,
            superseded_by=current.id,
            revised_at=AS_OF - day,
            created_at=AS_OF - 40 * day,
        ),
        item(
            "report_away",
            "The quarterly report for the other client lives in client/report.md",
            workspace_id=AWAY,
        ),
        item(
            "invoice_note_expired",
            "Invoice batch 7 was half imported",
            kind=MemoryKind.EPISODIC,
            basis=MemoryBasis.REPORTED,
            provenance=Provenance(kind=SourceKind.TASK, ref="task-7", label="import invoices"),
            expires_at=AS_OF - day,
            created_at=AS_OF - 200 * day,
        ),
        item(
            "invoice_parsing_analyst",
            "Invoices parse cleanly with the csv reader",
            scope=MemoryScope.EMPLOYEE_PRIVATE,
            employee_id=ANALYST,
            basis=MemoryBasis.OBSERVED,
            provenance=Provenance(kind=SourceKind.TASK, ref="task-1", label="parse invoices"),
        ),
        item(
            "invoice_parsing_writer",
            "Invoices are summarised as a table",
            scope=MemoryScope.EMPLOYEE_PRIVATE,
            employee_id=WRITER,
            basis=MemoryBasis.OBSERVED,
            provenance=Provenance(kind=SourceKind.TASK, ref="task-2", label="write summary"),
        ),
        item(
            "plan_step",
            "Plan step one produced invoices.csv",
            scope=MemoryScope.PLAN,
            plan_id=PLAN,
            kind=MemoryKind.EPISODIC,
            basis=MemoryBasis.REPORTED,
            provenance=Provenance(kind=SourceKind.TASK, ref="task-3", label="step one"),
        ),
        item(
            "plan_step_other",
            "Plan step one produced refunds.csv",
            scope=MemoryScope.PLAN,
            plan_id=OTHER_PLAN,
            kind=MemoryKind.EPISODIC,
            basis=MemoryBasis.REPORTED,
            provenance=Provenance(kind=SourceKind.TASK, ref="task-4", label="step one"),
        ),
        item(
            "preference_markdown",
            "The user prefers: answers formatted in Markdown",
            scope=MemoryScope.USER,
            workspace_id=AWAY,
            provenance=Provenance(kind=SourceKind.OBJECTIVE, ref="obj-1", label="a request"),
        ),
        item(
            "backup_old",
            "The nightly backup runs at 01:00",
            kind=MemoryKind.EPISODIC,
            basis=MemoryBasis.REPORTED,
            provenance=Provenance(kind=SourceKind.TASK, ref="task-5", label="check backup"),
            created_at=AS_OF - 60 * day,
        ),
        item(
            "backup_recent",
            "The nightly backup runs at 03:00",
            kind=MemoryKind.EPISODIC,
            basis=MemoryBasis.REPORTED,
            provenance=Provenance(kind=SourceKind.TASK, ref="task-6", label="check backup"),
            created_at=AS_OF - day,
        ),
    ]
    return {str(entry.metadata["eval"]): entry for entry in items}


def cases() -> tuple[EvalCase, ...]:
    everything = frozenset({MemoryScope.WORKSPACE, MemoryScope.USER, MemoryScope.PLAN})

    def query(text: str, **extra) -> MemoryQuery:
        extra.setdefault("workspace_id", HOME)
        extra.setdefault("scopes", everything)
        return MemoryQuery(text=text, as_of=AS_OF, **extra)

    return (
        EvalCase(
            "the current answer is found",
            GROUNDEDNESS,
            query("where is the quarterly report"),
            expected=frozenset({"report_current"}),
        ),
        EvalCase(
            "a preference applies in every workspace",
            GROUNDEDNESS,
            query("how should answers be formatted"),
            expected=frozenset({"preference_markdown"}),
        ),
        EvalCase(
            "nothing unrelated fills the budget",
            GROUNDEDNESS,
            query("quarterly report location"),
            forbidden=frozenset({"backup_recent", "backup_old", "preference_markdown"}),
        ),
        EvalCase(
            "a superseded memory is not recalled",
            FRESHNESS,
            query("where is the quarterly report"),
            forbidden=frozenset({"report_superseded"}),
        ),
        EvalCase(
            "an expired memory is not recalled",
            FRESHNESS,
            query("invoice batch imported"),
            forbidden=frozenset({"invoice_note_expired"}),
        ),
        EvalCase(
            "the recent answer comes first",
            FRESHNESS,
            query("when does the nightly backup run"),
            expected=frozenset({"backup_recent"}),
            ordered=(("backup_recent", "backup_old"),),
        ),
        EvalCase(
            "another workspace is not recalled",
            ISOLATION,
            query("quarterly report"),
            forbidden=frozenset({"report_away"}),
        ),
        EvalCase(
            "another employee's notes are not recalled",
            ISOLATION,
            query(
                "invoices",
                scopes=frozenset({MemoryScope.EMPLOYEE_PRIVATE}),
                employee_id=ANALYST,
            ),
            expected=frozenset({"invoice_parsing_analyst"}),
            forbidden=frozenset({"invoice_parsing_writer"}),
        ),
        EvalCase(
            "a caller naming no employee reads no private notes",
            ISOLATION,
            query("invoices", scopes=frozenset({MemoryScope.EMPLOYEE_PRIVATE})),
            forbidden=frozenset({"invoice_parsing_analyst", "invoice_parsing_writer"}),
        ),
        EvalCase(
            "another plan's memory is not recalled",
            ISOLATION,
            query("plan step produced", plan_id=PLAN),
            expected=frozenset({"plan_step"}),
            forbidden=frozenset({"plan_step_other"}),
        ),
    )


async def run_retrieval_evals(
    make_memory: Callable[[], Memory], *, backend: str = ""
) -> RetrievalReport:
    """Seed a fresh store and ask every case of it."""
    memory = make_memory()
    seeded = corpus()
    for item in seeded.values():
        await memory.remember(item)
    by_id = {item.id: key for key, item in seeded.items()}

    results: list[CaseResult] = []
    for case in cases():
        found = await memory.recall(case.query)
        keys = tuple(by_id.get(item.id, f"unknown:{item.id}") for item in found)
        problems: list[str] = []
        for key in sorted(case.expected - set(keys)):
            problems.append(f"expected {key} was not recalled")
        for key in sorted(case.forbidden & set(keys)):
            problems.append(f"{key} was recalled and must not be")
        for earlier, later in case.ordered:
            if earlier in keys and later in keys and keys.index(earlier) > keys.index(later):
                problems.append(f"{later} ranked above {earlier}")
        if case.metric == GROUNDEDNESS:
            for item in found:
                if item.provenance.kind is SourceKind.UNKNOWN:
                    problems.append(f"{by_id.get(item.id, item.id)} has no traceable source")
        results.append(CaseResult(case=case, returned=keys, problems=tuple(problems)))
    return RetrievalReport(backend=backend, results=tuple(results))


def render_report(report: RetrievalReport) -> str:
    lines = [f"Retrieval evals ({report.backend or 'memory'})"]
    scores = report.scores
    for metric, floor in report.thresholds.items():
        mark = "ok" if scores[metric] >= floor else "BELOW"
        lines.append(f"  {metric:<13} {scores[metric]:.2f} (threshold {floor:.2f}) {mark}")
    for result in report.results:
        if not result.passed:
            lines.append(f"  FAILED [{result.case.metric}] {result.case.name}")
            lines.extend(f"    - {problem}" for problem in result.problems)
    lines.append("PASSED" if report.passed else "FAILED")
    return "\n".join(lines)
