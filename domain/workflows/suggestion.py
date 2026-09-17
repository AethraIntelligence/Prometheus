"""Noticing that the same process keeps being planned, and offering to keep it.

A workflow is what somebody already knows a request takes (ADR 0011's sibling
rule: a declaration, not code). The manager rediscovers that knowledge every
time, at the price of a plan, and the only record that it has done so three
times is three plans nobody compares. This compares them.

**The pattern is structure, never text.** Two requests worded alike can need
different work, and two worded differently can need the same - "summarise the
notes" and "leave me a digest of notes/" are one process. So a run's signature
is who did each step, what each step needed and how the steps depend on each
other. The request's words are not part of it and are not copied into a draft:
a suggestion that quoted three people's requests back would be storing them a
second time for no reason the process needs.

**Evidence decides, and only good evidence.** A pattern counts only runs whose
objective finished DONE with every step accepted, in one workspace, inside a
window; it is offered at `MIN_OCCURRENCES` distinct objectives and at least
`MIN_STEPS` steps (a one-step "process" is a request). One suggestion per
fingerprint per workspace.

**A person's no is kept.** Dismissing records the objectives it was dismissed
on, and the suggestion comes back only when that many *new* successful runs
have happened since - the evidence changed, not the page reloaded. Snoozing
hides it until a time. Saving is its own confirmation, and nothing a suggestion
does activates, schedules or grants anything.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from domain.workflows.definition import (
    InputKind,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowStep,
    WorkflowTrigger,
)
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

MIN_OCCURRENCES = 3
MIN_STEPS = 2
DEFAULT_WINDOW = timedelta(days=30)
PATTERN_VERSION = 1


class SuggestionStatus(StrEnum):
    OPEN = "OPEN"
    DISMISSED = "DISMISSED"
    SNOOZED = "SNOOZED"
    SAVED = "SAVED"


@dataclass(frozen=True, slots=True)
class StepShape:
    """One step of a process, as structure: who, needing what, after which steps."""

    employee: str
    needs: tuple[str, ...] = ()
    depends_on: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee": self.employee,
            "needs": list(self.needs),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StepShape:
        return cls(
            employee=str(raw.get("employee", "")),
            needs=tuple(str(item) for item in raw.get("needs") or ()),
            depends_on=tuple(int(item) for item in raw.get("depends_on") or ()),
        )


@dataclass(frozen=True, slots=True)
class SuccessfulRun:
    """One objective that finished well, reduced to its structure."""

    objective_id: UUID
    finished_at: datetime
    steps: tuple[StepShape, ...]

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.steps)


def fingerprint(steps: Iterable[StepShape]) -> str:
    payload = json.dumps(
        {"version": PATTERN_VERSION, "steps": [step.to_dict() for step in steps]},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Pattern:
    fingerprint: str
    steps: tuple[StepShape, ...]
    sources: tuple[UUID, ...]
    first_seen: datetime
    last_seen: datetime


def patterns(
    runs: Iterable[SuccessfulRun],
    *,
    now: datetime,
    window: timedelta = DEFAULT_WINDOW,
) -> list[Pattern]:
    """Every structure that recurred often enough inside the window."""
    grouped: dict[str, list[SuccessfulRun]] = {}
    for run in runs:
        if len(run.steps) < MIN_STEPS or not now - window <= run.finished_at <= now:
            continue
        grouped.setdefault(run.fingerprint, []).append(run)
    found: list[Pattern] = []
    for key, members in grouped.items():
        distinct = {run.objective_id: run for run in members}
        if len(distinct) < MIN_OCCURRENCES:
            continue
        ordered = sorted(distinct.values(), key=lambda run: run.finished_at)
        found.append(
            Pattern(
                fingerprint=key,
                steps=ordered[0].steps,
                sources=tuple(run.objective_id for run in ordered),
                first_seen=ordered[0].finished_at,
                last_seen=ordered[-1].finished_at,
            )
        )
    return sorted(found, key=lambda pattern: pattern.last_seen, reverse=True)


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: UUID
    fingerprint: str
    steps: tuple[StepShape, ...]
    sources: tuple[UUID, ...]
    first_seen: datetime
    last_seen: datetime
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    status: SuggestionStatus = SuggestionStatus.OPEN
    #: The objectives it was dismissed on. It returns only on as many new ones.
    dismissed_sources: tuple[UUID, ...] = ()
    snoozed_until: datetime | None = None
    workflow_name: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def occurrences(self) -> int:
        return len(self.sources)

    def visible(self, now: datetime) -> bool:
        """Whether a person should be shown it now."""
        if self.status is SuggestionStatus.OPEN:
            return self.occurrences >= MIN_OCCURRENCES
        return False

    def dismissed(self, now: datetime) -> Suggestion:
        return replace(
            self,
            status=SuggestionStatus.DISMISSED,
            dismissed_sources=tuple(dict.fromkeys((*self.dismissed_sources, *self.sources))),
            snoozed_until=None,
            updated_at=now,
        )

    def snoozed(self, until: datetime, now: datetime) -> Suggestion:
        return replace(self, status=SuggestionStatus.SNOOZED, snoozed_until=until, updated_at=now)

    def saved(self, workflow_name: str, now: datetime) -> Suggestion:
        return replace(
            self, status=SuggestionStatus.SAVED, workflow_name=workflow_name, updated_at=now
        )


def reconcile(
    existing: Suggestion | None,
    pattern: Pattern | None,
    *,
    workspace_id: WorkspaceId,
    now: datetime,
) -> Suggestion | None:
    """The stored suggestion as the current evidence leaves it.

    `pattern` None means the structure no longer recurs often enough inside the
    window; an existing suggestion keeps its status and loses its sources, so it
    is not shown - and is not re-created as new when the evidence returns.
    """
    if existing is None:
        if pattern is None:
            return None
        return Suggestion(
            # The same workspace and structural pattern always get the same id.
            # Two windows refreshing concurrently therefore upsert one row
            # instead of racing two random ids against the unique fingerprint.
            id=uuid5(
                NAMESPACE_URL,
                f"prometheus:workflow-suggestion:{workspace_id}:{pattern.fingerprint}",
            ),
            fingerprint=pattern.fingerprint,
            steps=pattern.steps,
            sources=pattern.sources,
            first_seen=pattern.first_seen,
            last_seen=pattern.last_seen,
            workspace_id=workspace_id,
            created_at=now,
            updated_at=now,
        )

    sources = pattern.sources if pattern else ()
    current = replace(
        existing,
        sources=sources,
        last_seen=pattern.last_seen if pattern else existing.last_seen,
    )
    if current.status is SuggestionStatus.DISMISSED:
        fresh = [source for source in sources if source not in set(existing.dismissed_sources)]
        if len(fresh) >= MIN_OCCURRENCES:
            current = replace(current, status=SuggestionStatus.OPEN)
    elif current.status is SuggestionStatus.SNOOZED:
        if existing.snoozed_until is None or now >= existing.snoozed_until:
            current = replace(current, status=SuggestionStatus.OPEN, snoozed_until=None)
    if current != existing:
        current = replace(current, updated_at=now)
    return current


def draft(suggestion: Suggestion, name: str, description: str = "") -> WorkflowDefinition:
    """The workflow a saved suggestion becomes: manual, versioned 1, no schedule.

    Instructions are placeholders over one input, `request`, filled at run time.
    Nothing of the requests the pattern was found in is copied into it.
    """
    names = [f"step-{index + 1}" for index in range(len(suggestion.steps))]
    steps = []
    for index, shape in enumerate(suggestion.steps):
        earlier = [names[item] for item in shape.depends_on if 0 <= item < len(names)]
        needs = f" It needs {', '.join(shape.needs)}." if shape.needs else ""
        if earlier:
            handed = "; ".join(f"{{steps.{step}}}" for step in earlier)
            text = (
                f"Continue this request: {{request}}. "
                f"Work from the earlier result: {handed}.{needs}"
            )
        else:
            text = f"Do the first part of this request: {{request}}.{needs}"
        steps.append(
            WorkflowStep(
                name=names[index],
                employee=shape.employee,
                instruction=text,
                depends_on=tuple(earlier),
            )
        )
    return WorkflowDefinition(
        name=name,
        version=1,
        description=description
        or f"Saved from a pattern seen in {suggestion.occurrences} successful runs.",
        trigger=WorkflowTrigger.MANUAL,
        steps=tuple(steps),
        input_schema={
            "request": WorkflowInput(
                kind=InputKind.STRING, required=True, description="What to do this time."
            )
        },
    )


class SuggestionRepository(Protocol):
    async def save(self, suggestion: Suggestion) -> None: ...

    async def get(self, suggestion_id: UUID) -> Suggestion | None: ...

    async def list(self, workspace_id: WorkspaceId) -> list[Suggestion]: ...


class WorkflowWriter(Protocol):
    """Where a confirmed draft is written. Never overwrites an existing workflow."""

    def write(self, definition: WorkflowDefinition) -> str:
        """Persist it and return where; raise if the name is taken."""
        ...
