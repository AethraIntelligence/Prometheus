"""What is written down, and what is deliberately not.

Memory is expensive in the only currency a run has: every remembered line is a
line of context a later run pays for reading. So this writes four things, each
answering a question a later run actually asks.

* **What happened to a task** (EPISODIC, workspace) - what is now true, and the
  goal it came out of. What the employee *said* is not what is stored: it is put
  through `OutcomeDistiller` first, because a model's closing message is written
  for the person who asked and reads as narration a month later. A failure is
  worth as much as a success here and is kept at a lower importance: knowing
  that an approach did not work is what stops it being tried a third time.
* **What the plan learned** (EPISODIC, plan) - the same outcome, scoped to the
  plan, so the task that follows this one in the same objective reads it while
  an unrelated objective next week does not.
* **What this employee learned** (SEMANTIC, private) - which interfaces and
  tools actually reached the work. It is private because it is about how *this*
  employee works, and telling another employee would be advice from someone
  else's machine.
* **How the user wants things done** (SEMANTIC, user) - the standing
  preferences Prometheus read out of a request, and *only* those. These are the
  memories with no expiry, because a preference does not stop being true on a
  timer - which is exactly why what goes in here has to be a preference. A
  request's own parameters ("the sales folder", "call it summary.md") look
  identical and are not: kept here, they come back as workspace facts and point
  the next request at the last one's folder. That is not a hypothetical; it is
  what the first validation run did (validation/tasks/phase-09-*).

Two rules hold throughout. **A failure to remember never fails the work**: every
write is guarded, and a run whose memory backend is down is a run that goes
slightly worse, not a run that stops. And **nothing is written that was not
already safe to persist**: the content comes from summaries that have been
through `redact`, and tool *arguments* are never stored - only names.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from application.memory.consolidation import Consolidator
from application.memory.distiller import OutcomeDistiller
from application.memory.revision import MemoryReviser
from domain.employees.definition import EmployeeDefinition
from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    Provenance,
    SourceKind,
)
from domain.memory.protocols import Memory
from domain.memory.ranking import expires_at
from domain.tasks.plan import Observation
from domain.tasks.task import Task, TaskStatus
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How much of a result is kept. Memory is a pointer to what happened, not a
#: second copy of the transcript - the task row already holds the whole of it.
MAX_CONTENT = 800

IMPORTANCE_BY_STATUS = {
    TaskStatus.COMPLETED: 0.7,
    TaskStatus.FAILED: 0.45,
    TaskStatus.CANCELLED: 0.2,
}

#: How far an employee's account of a task is to be trusted. A completed task
#: passed its verifier; a failed one is still a report, of a failure.
CONFIDENCE_BY_STATUS = {
    TaskStatus.COMPLETED: 0.75,
    TaskStatus.FAILED: 0.5,
    TaskStatus.CANCELLED: 0.3,
}


def trim(text: str, limit: int = MAX_CONTENT) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


class MemoryRecorder:
    """Writes what is worth keeping, and never raises at the caller."""

    def __init__(
        self,
        memory: Memory,
        *,
        consolidator: Consolidator | None = None,
        distiller: OutcomeDistiller | None = None,
        reviser: MemoryReviser | None = None,
    ) -> None:
        self._memory = memory
        self._consolidator = consolidator
        self._distiller = distiller
        # Standing memories only: a task's outcome is episodic and expires, and
        # a model call per task to check it against the past is a cost every
        # run would pay for a question decay already answers.
        self._reviser = reviser

    # --- During a run ---------------------------------------------------------

    async def note_step(
        self, task: Task, definition: EmployeeDefinition, observation: Observation
    ) -> None:
        """Working memory: what this employee just saw, for the rest of this task.

        Written with a short time to live and scoped to the task, because it is
        the only kind of memory that is worthless once the task ends - and left
        unexpired it would be the most numerous kind by a wide margin.
        """
        await self.remember(
            MemoryItem.create(
                trim(f"Step {observation.step}: {observation.summary}", 400),
                scope=MemoryScope.EMPLOYEE_PRIVATE,
                kind=MemoryKind.WORKING,
                workspace_id=task.workspace_id,
                employee_id=definition.id,
                task_id=task.id,
                plan_id=task.plan_id,
                importance=0.3 if observation.succeeded else 0.5,
                expires_at=expires_at(MemoryKind.WORKING),
                metadata={"tool": observation.details.get("tool", "")},
                basis=MemoryBasis.OBSERVED if observation.succeeded else MemoryBasis.REPORTED,
                confidence=0.8 if observation.succeeded else 0.5,
                provenance=Provenance(
                    kind=SourceKind.TASK, ref=str(task.id), label=trim(task.goal, 160)
                ),
            )
        )

    # --- After a run ----------------------------------------------------------

    async def record_task(self, task: Task, definition: EmployeeDefinition) -> None:
        """The task, its outcome, and what the employee learned doing it."""
        # What was found out comes first and what was asked comes second. Both
        # are needed - the goal is what makes the memory findable - but a recall
        # that leads with the goal reads as a list of jobs somebody did, and a
        # model given that plans archaeology: it sets out to reconstruct the
        # earlier work instead of using what the earlier work found. The first
        # validation run spent a whole budget doing exactly that.
        summary = task.result.summary if task.result else ""
        found = summary or (task.error.message if task.error else "no result")
        if self._distiller is not None:
            # What the employee said is written to be read now, by the person
            # who asked. What is stored has to be readable in a month by someone
            # who was not there, which is a different piece of text.
            found = await self._distiller.distil(task, found)
        found = trim(found)
        outcome = (
            f"{found}\n(Found while doing: {trim(task.goal, 160)} - {task.status.value})"
        )
        importance = IMPORTANCE_BY_STATUS.get(task.status, 0.4)
        common = {
            "workspace_id": task.workspace_id,
            "task_id": task.id,
            "plan_id": task.plan_id,
            "importance": importance,
            "metadata": {"employee": definition.name, "status": task.status.value},
            "provenance": Provenance(
                kind=SourceKind.TASK, ref=str(task.id), label=trim(task.goal, 160)
            ),
        }

        await self.remember(
            MemoryItem.create(
                outcome,
                scope=MemoryScope.WORKSPACE,
                kind=MemoryKind.EPISODIC,
                expires_at=expires_at(MemoryKind.EPISODIC),
                # What the employee said happened, checked by a verifier at
                # most: a claim, and shown to later runs as one.
                basis=MemoryBasis.REPORTED,
                confidence=CONFIDENCE_BY_STATUS.get(task.status, 0.4),
                **common,
            )
        )
        if task.plan_id is not None:
            # The same outcome, readable by the tasks that come after this one
            # inside the same plan and by nothing else.
            await self.remember(
                MemoryItem.create(
                    outcome,
                    scope=MemoryScope.PLAN,
                    kind=MemoryKind.EPISODIC,
                    expires_at=expires_at(MemoryKind.EPISODIC),
                    basis=MemoryBasis.REPORTED,
                    confidence=CONFIDENCE_BY_STATUS.get(task.status, 0.4),
                    **{**common, "importance": min(importance + 0.1, 1.0)},
                )
            )

        lesson = _lesson(task)
        if lesson:
            await self.remember(
                MemoryItem.create(
                    lesson,
                    scope=MemoryScope.EMPLOYEE_PRIVATE,
                    kind=MemoryKind.SEMANTIC,
                    employee_id=definition.id,
                    # Read off the recorded tool calls, not off anybody's account.
                    basis=MemoryBasis.OBSERVED,
                    confidence=0.9,
                    **common,
                )
            )

        if self._consolidator is not None:
            await self._consolidator.consolidate(workspace_id=task.workspace_id)

    async def record_preferences(
        self,
        preferences: Sequence[str],
        *,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        source: str = "",
        objective_id: str = "",
    ) -> None:
        """How the user wants work done here, from now on (§9.4).

        Sentences, not a mapping, and one item each: they are recalled
        separately, they stop being true separately, and one item holding six of
        them is six things that can never be forgotten one at a time.

        Whether something *is* a standing preference is decided when the request
        is read, not here - see `prompts/prometheus_intent/v1.md`. This writes what it
        is given, which is why what it is given matters so much.

        Written at USER scope since Phase 15: a preference is about the person,
        not about the context they happened to state it in, and one that had to
        be re-stated per workspace would be re-stated in none of them. The
        workspace it was said in is kept as provenance, so narrowing one to a
        single context later is an edit rather than a migration.
        """
        for preference in preferences:
            stated = trim(str(preference), 300)
            if not stated:
                continue
            item = MemoryItem.create(
                f"The user prefers: {stated}",
                scope=MemoryScope.USER,
                kind=MemoryKind.SEMANTIC,
                workspace_id=workspace_id,
                # No expiry: a preference is superseded by another
                # preference, not by time passing - which is what the
                # reviser does when one is stated.
                importance=0.8,
                metadata={
                    "source": trim(source, 200),
                    "stated_in": str(workspace_id),
                },
                # Stated by the person, but read out of their request by a
                # model: evidence, one reading removed.
                basis=MemoryBasis.STATED,
                confidence=0.8,
                provenance=Provenance(
                    kind=SourceKind.OBJECTIVE, ref=objective_id, label=trim(source, 200)
                ),
            )
            await self.remember_standing(item)

    # --- Writing --------------------------------------------------------------

    async def remember_standing(self, item: MemoryItem) -> MemoryItem:
        """A memory with no expiry, checked against what it may replace."""
        if not item.content.strip():
            return item
        if self._reviser is None:
            await self.remember(item)
            return item
        return await self._reviser.remember(item)

    async def remember(self, item: MemoryItem) -> None:
        """Write one item, or log why it could not be written. Never raises."""
        if not item.content.strip():
            return
        try:
            await self._memory.remember(item)
        except Exception as error:  # remembering must not be able to fail a run
            log.warning("memory.write_failed", kind=item.kind.value, error=str(error))


def _lesson(task: Task) -> str:
    """What the employee should know next time, from what the run actually did.

    Read off the observations rather than asked of a model: this runs after
    every task, and a model call per task to summarise what a list already says
    is a cost with no answer behind it.
    """
    observations = (task.result.output.get("observations") if task.result else None) or ()
    worked: list[str] = []
    failed: list[str] = []
    for raw in observations:
        if not isinstance(raw, dict):
            continue
        tool = str((raw.get("details") or {}).get("tool", "")).strip()
        if not tool:
            continue
        (worked if raw.get("succeeded", True) else failed).append(tool)

    if not worked and not failed:
        return ""
    goal = trim(task.goal, 160)
    parts = [f"Working on '{goal}':"]
    if worked:
        parts.append("these tools got there: " + ", ".join(sorted(set(worked))) + ".")
    if failed:
        parts.append("these did not: " + ", ".join(sorted(set(failed))) + ".")
    return " ".join(parts)
