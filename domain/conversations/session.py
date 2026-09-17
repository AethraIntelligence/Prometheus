"""What a long thread has established, in a size that does not grow with it.

A thread used to reach the manager as its last eight turns. That is exact and
bounded, and it forgets: the decision made in turn three of forty is not in the
last eight, and neither is the file turn five wrote. Sending every turn instead
keeps the decision and sends the thread's whole history to every model call.

So a thread has a *brief*, in two halves that are kept apart on purpose.

**Projected** - decisions, open questions and the artifact index are read off
the objectives every time they are asked for, never stored as a second copy.
A request's constraints are the decisions it made; what an escalated answer
said was missing is a question still open; the files the work wrote are the
artifacts. A projection cannot drift from the record, survives a restart by
construction, and two requests finishing at once cannot overwrite each other's
half of it.

**Compacted** - what a model had to write: a goal brief and one summary per
*stage*, a run of older turns folded together. These are the only stored part,
and each stage names the objectives it covers, so compaction is idempotent and
a turn is folded at most once. The objectives themselves are never touched:
compaction shortens what a model is shown, not what happened (the audit trail).

The rules deciding *when* to compact and *what* fits are pure functions here,
because they are the part a test should be able to state exactly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from domain.workforce.protocols import Objective, ObjectiveStatus
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

#: Turns kept verbatim at the end of the thread. Enough for "use the second
#: option"; older turns are what stages are for.
RECENT_TURNS = 6

#: How many older turns one stage folds. Compaction waits until there are this
#: many, so its model call is paid once per stage rather than once per turn.
STAGE_SIZE = 6

#: The whole thread context, in characters, however long the thread is.
CONTEXT_CHARS = 12_000

#: The share of that the brief may take before the recent turns are fitted.
BRIEF_SHARE = 0.4

MAX_DECISIONS = 12
MAX_QUESTIONS = 8
MAX_ARTIFACTS = 15
#: Stage summaries shown. Older ones are covered by the goal brief, which every
#: compaction rewrites with the stage it folded.
MAX_STAGES = 5


@dataclass(frozen=True, slots=True)
class SessionStage:
    """Several older turns, folded into one summary."""

    index: int
    summary: str
    objective_ids: tuple[UUID, ...]
    started_at: datetime
    ended_at: datetime
    compacted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Whether a model wrote the summary. False means the fallback did, which
    #: is shorter and worse and still bounded.
    summarised: bool = True


@dataclass(frozen=True, slots=True)
class SessionState:
    """The stored half of a thread's brief."""

    conversation_id: UUID
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    goal_brief: str = ""
    stages: tuple[SessionStage, ...] = ()
    #: Open questions somebody marked as settled, by their text.
    resolved_questions: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def compacted(self) -> frozenset[UUID]:
        return frozenset(i for stage in self.stages for i in stage.objective_ids)


@dataclass(frozen=True, slots=True)
class SessionNote:
    """A decision or an open question, and the request it came from."""

    text: str
    objective_id: UUID
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class SessionArtifact:
    path: str
    objective_id: UUID
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class SessionTurn:
    objective_id: UUID
    request: str
    answer: str
    status: str
    at: datetime


@dataclass(frozen=True, slots=True)
class SessionBrief:
    """The whole brief: projected, compacted, and the turns still verbatim."""

    conversation_id: UUID
    goal: str
    decisions: tuple[SessionNote, ...] = ()
    open_questions: tuple[SessionNote, ...] = ()
    artifacts: tuple[SessionArtifact, ...] = ()
    stages: tuple[SessionStage, ...] = ()
    recent: tuple[SessionTurn, ...] = ()
    total_turns: int = 0

    @property
    def compacted_turns(self) -> int:
        return sum(len(stage.objective_ids) for stage in self.stages)


def turns(objectives: Iterable[Objective]) -> list[SessionTurn]:
    """The answered requests of a thread, oldest first."""
    return [
        SessionTurn(
            objective_id=item.id,
            request=item.text,
            answer=item.result.summary,
            status=item.status.value,
            at=item.finished_at or item.created_at,
        )
        for item in objectives
        if item.result is not None
    ]


def project(
    state: SessionState | None,
    conversation_id: UUID,
    objectives: Sequence[Objective],
    artifacts: Mapping[UUID, Sequence[str]] | None = None,
    *,
    exclude: UUID | None = None,
    recent_turns: int = RECENT_TURNS,
) -> SessionBrief:
    """The brief of a thread, as of these objectives.

    `exclude` is the request being worked on now: it is not yet part of what the
    thread established, and showing it to itself as history is noise.
    """
    thread = [item for item in objectives if item.id != exclude]
    stored = state or SessionState(conversation_id=conversation_id)
    answered = turns(thread)
    compacted = stored.compacted

    decisions: dict[str, SessionNote] = {}
    questions: dict[str, SessionNote] = {}
    resolved = {_key(text) for text in stored.resolved_questions}
    files: dict[str, SessionArtifact] = {}
    for item in thread:
        at = item.finished_at or item.created_at
        # What a request pinned down - the folder, the format, the name - is a
        # decision of this thread. The later request wins on the same key: a
        # thread that changed its mind has one current decision, not two.
        for key, value in item.constraints.items():
            if value in (None, "", [], {}):
                continue
            decisions[_key(str(key))] = SessionNote(f"{key}: {_value(value)}", item.id, at)
        if item.result is not None and item.status is not ObjectiveStatus.DONE:
            for missing in item.result.missing:
                if missing.strip() and _key(missing) not in resolved:
                    questions.setdefault(_key(missing), SessionNote(missing.strip(), item.id, at))
        for path in (artifacts or {}).get(item.id, ()):
            files[path] = SessionArtifact(path, item.id, at)

    goal = stored.goal_brief.strip() or (thread[0].text.strip() if thread else "")
    uncompacted = [turn for turn in answered if turn.objective_id not in compacted]
    return SessionBrief(
        conversation_id=conversation_id,
        goal=goal,
        decisions=tuple(sorted(decisions.values(), key=lambda n: n.recorded_at)),
        open_questions=tuple(sorted(questions.values(), key=lambda n: n.recorded_at)),
        artifacts=tuple(sorted(files.values(), key=lambda a: a.recorded_at)),
        stages=stored.stages,
        recent=tuple(uncompacted[-recent_turns:]) if recent_turns > 0 else (),
        total_turns=len(answered),
    )


def due_for_compaction(
    state: SessionState | None,
    objectives: Sequence[Objective],
    *,
    recent_turns: int = RECENT_TURNS,
    stage_size: int = STAGE_SIZE,
) -> list[Objective]:
    """The oldest uncompacted turns, once a whole stage of them sits outside the window.

    Empty means not yet. Only answered requests count, and never the most
    recent ones: those are still shown verbatim and are what the next request is
    most likely about.
    """
    compacted = state.compacted if state else frozenset()
    answered = [item for item in objectives if item.result is not None and item.is_terminal]
    pending = [item for item in answered if item.id not in compacted]
    older = pending[: max(len(pending) - recent_turns, 0)]
    return older[:stage_size] if len(older) >= stage_size else []


def with_stage(
    state: SessionState | None,
    conversation_id: UUID,
    folded: Sequence[Objective],
    *,
    summary: str,
    goal_brief: str = "",
    resolved: Sequence[str] = (),
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    summarised: bool = True,
    now: datetime | None = None,
) -> SessionState:
    """The state after one more stage. Folding a turn twice is refused, not repeated."""
    moment = now or datetime.now(UTC)
    current = state or SessionState(conversation_id=conversation_id, workspace_id=workspace_id)
    fresh = [item for item in folded if item.id not in current.compacted]
    if not fresh:
        return current
    stage = SessionStage(
        index=len(current.stages) + 1,
        summary=summary.strip(),
        objective_ids=tuple(item.id for item in fresh),
        started_at=min(item.created_at for item in fresh),
        ended_at=max(item.finished_at or item.created_at for item in fresh),
        compacted_at=moment,
        summarised=summarised,
    )
    return SessionState(
        conversation_id=current.conversation_id,
        workspace_id=current.workspace_id,
        goal_brief=goal_brief.strip() or current.goal_brief,
        stages=(*current.stages, stage),
        resolved_questions=_merge(current.resolved_questions, resolved),
        updated_at=moment,
    )


def resolving(
    state: SessionState | None,
    conversation_id: UUID,
    question: str,
    *,
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
) -> SessionState:
    current = state or SessionState(
        conversation_id=conversation_id, workspace_id=workspace_id
    )
    return SessionState(
        conversation_id=current.conversation_id,
        workspace_id=current.workspace_id,
        goal_brief=current.goal_brief,
        stages=current.stages,
        resolved_questions=_merge(current.resolved_questions, (question,)),
        updated_at=datetime.now(UTC),
    )


def render(brief: SessionBrief, *, budget: int = CONTEXT_CHARS) -> tuple[str, ...]:
    """The brief as context lines, never longer than `budget` characters in total.

    The brief comes first and is capped at a share of the budget; the recent
    turns fill what is left, newest first, so a very long last answer costs the
    older turns their place rather than the brief its.
    """
    lines: list[str] = []
    if brief.total_turns or brief.decisions or brief.artifacts:
        sections = [f"Session brief for this thread ({brief.total_turns} earlier request(s))."]
        if brief.goal:
            sections.append(f"Goal: {_clip(brief.goal, 600)}")
        if brief.decisions:
            sections.append(
                "Decisions made in this thread:\n"
                + "\n".join(f"- {note.text}" for note in brief.decisions[-MAX_DECISIONS:])
            )
        if brief.open_questions:
            sections.append(
                "Still open (reported missing, not settled since):\n"
                + "\n".join(f"- {note.text}" for note in brief.open_questions[-MAX_QUESTIONS:])
            )
        if brief.artifacts:
            sections.append(
                "Files this thread produced:\n"
                + "\n".join(f"- {item.path}" for item in brief.artifacts[-MAX_ARTIFACTS:])
            )
        if brief.stages:
            shown = brief.stages[-MAX_STAGES:]
            omitted = len(brief.stages) - len(shown)
            header = "Earlier stages, compacted"
            if omitted:
                header += f" ({omitted} older stage(s) are covered by the goal)"
            sections.append(
                header
                + ":\n"
                + "\n".join(
                    f"- Stage {stage.index} ({len(stage.objective_ids)} request(s)): "
                    f"{_clip(stage.summary, 700)}"
                    for stage in shown
                )
            )
        text = _clip("\n\n".join(sections), int(budget * BRIEF_SHARE))
        lines.append(text)

    remaining = budget - sum(len(line) for line in lines)
    chosen: list[str] = []
    for turn in reversed(brief.recent):
        if remaining <= 0:
            break
        text = f"Earlier in this thread:\nUser: {turn.request}\nPrometheus: {turn.answer}".strip()
        text = text[:remaining].rstrip()
        if not text:
            break
        chosen.append(text)
        remaining -= len(text)
    chosen.reverse()
    return (*lines, *chosen)


def _key(text: str) -> str:
    return " ".join(text.lower().split())


def _value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part) for part in value)
    return _clip(str(value), 200)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def _merge(existing: Sequence[str], added: Iterable[str]) -> tuple[str, ...]:
    seen = {_key(text) for text in existing}
    merged = list(existing)
    for text in added:
        if text.strip() and _key(text) not in seen:
            merged.append(text.strip())
            seen.add(_key(text))
    return tuple(merged)
