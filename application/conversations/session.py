"""A thread's brief, read for the manager and compacted after each answer.

The rules - what is projected, when a stage is due, what fits - are the
domain's (`domain/conversations/session.py`). This is the part with a store and
a model behind it: reading a thread's objectives and the files its work wrote,
and folding a stage of older turns into a summary.

Everything here is guarded the way memory is. A brief that cannot be read costs
the next request its view of the thread's history, not the request; a
compaction that fails leaves the turns uncompacted, which is a longer context
next time rather than a lost one - the recent-turn window and the character
budget still bound it.

Compaction has a fallback that needs no model. It is worse - the first words of
each request and answer rather than what they settled - and it is still a
stage, so a machine whose summariser is unreachable keeps a bounded context
instead of an ever longer list of turns waiting for a model that never comes.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import structlog

from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.conversations import session as rules
from domain.conversations.repository import SessionStateRepository
from domain.conversations.session import SessionBrief, SessionState
from domain.llm.json_output import extract_object
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.protocols import LLM
from domain.tools.artifacts import produced_files
from domain.tools.telemetry import ToolCallLog
from domain.workforce.protocols import Objective
from domain.workforce.repository import ObjectiveRepository, PlanRepository
from domain.workspace.models import DEFAULT_WORKSPACE_ID

log = structlog.get_logger(__name__)

#: How much of one turn a compaction prompt shows. The summary is four
#: sentences; a turn's whole answer would make the prompt the long context this
#: exists to avoid.
TURN_CHARS = 1_500


class SessionMemory:
    """The brief of each thread, for the manager to read and keep short."""

    def __init__(
        self,
        *,
        objectives: ObjectiveRepository,
        states: SessionStateRepository,
        plans: PlanRepository | None = None,
        tool_calls: ToolCallLog | None = None,
        llm: LLM | None = None,
        recent_turns: int = rules.RECENT_TURNS,
        stage_size: int = rules.STAGE_SIZE,
        budget: int = rules.CONTEXT_CHARS,
    ) -> None:
        self._objectives = objectives
        self._states = states
        self._plans = plans
        self._tool_calls = tool_calls
        self._llm = llm
        self._recent_turns = recent_turns
        self._stage_size = stage_size
        self._budget = budget

    async def brief(
        self, conversation_id: UUID, *, exclude: UUID | None = None
    ) -> SessionBrief:
        thread = await self._objectives.for_conversation(conversation_id)
        state = await self._states.get(conversation_id)
        return rules.project(
            state,
            conversation_id,
            thread,
            await self._artifacts(thread),
            exclude=exclude,
            recent_turns=self._recent_turns,
        )

    async def context_for(self, objective: Objective) -> tuple[str, ...]:
        """The thread as the manager should see it before this request."""
        if objective.conversation_id is None:
            return ()
        try:
            brief = await self.brief(objective.conversation_id, exclude=objective.id)
        except Exception as error:
            log.warning("session.brief_unreadable", error=str(error))
            return ()
        lines = rules.render(brief, budget=self._budget)
        if brief.total_turns:
            log.info(
                "session.brief_read",
                objective_id=str(objective.id),
                turns=brief.total_turns,
                compacted=brief.compacted_turns,
                chars=sum(len(line) for line in lines),
            )
        return lines

    async def after(self, objective: Objective) -> SessionState | None:
        """Compact the thread if a stage has become due. Never raises."""
        if objective.conversation_id is None:
            return None
        try:
            return await self._compact(objective.conversation_id)
        except Exception as error:
            log.warning("session.compaction_failed", error=str(error))
            return None

    async def resolve(self, conversation_id: UUID, question: str) -> SessionBrief:
        """A person saying an open question is settled."""
        brief = await self.brief(conversation_id)
        wanted = " ".join(question.lower().split())
        if not any(" ".join(note.text.lower().split()) == wanted for note in brief.open_questions):
            raise ValueError("That question is not open in this thread.")
        state = await self._states.get(conversation_id)
        thread = await self._objectives.for_conversation(conversation_id)
        workspace_id = thread[0].workspace_id if thread else DEFAULT_WORKSPACE_ID
        await self._states.save(
            rules.resolving(
                state,
                conversation_id,
                question,
                workspace_id=workspace_id,
            )
        )
        return await self.brief(conversation_id)

    async def _compact(self, conversation_id: UUID) -> SessionState | None:
        thread = await self._objectives.for_conversation(conversation_id)
        state = await self._states.get(conversation_id)
        written: SessionState | None = None
        # More than one stage can be due after a restart, or on a thread that
        # predates briefs: folded one at a time, oldest first, each its own save.
        while folded := rules.due_for_compaction(
            state, thread, recent_turns=self._recent_turns, stage_size=self._stage_size
        ):
            brief = rules.project(state, conversation_id, thread, recent_turns=0)
            summary, goal, resolved, summarised = await self._summarise(
                brief, folded, thread
            )
            state = rules.with_stage(
                state,
                conversation_id,
                folded,
                summary=summary,
                goal_brief=goal,
                resolved=resolved,
                workspace_id=folded[0].workspace_id,
                summarised=summarised,
            )
            await self._states.save(state)
            written = state
            log.info(
                "session.compacted",
                conversation_id=str(conversation_id),
                stage=len(state.stages),
                turns=len(folded),
                summarised=summarised,
            )
        return written

    async def _summarise(
        self, brief: SessionBrief, folded: Sequence[Objective], thread: Sequence[Objective]
    ) -> tuple[str, str, tuple[str, ...], bool]:
        turns = rules.turns(folded)
        if self._llm is not None:
            try:
                response = await self._llm.generate(
                    LLMRequest(
                        messages=(
                            Message.user(
                                render(
                                    "session_compaction",
                                    goal_brief=brief.goal or "(none yet)",
                                    open_questions="\n".join(
                                        f"- {note.text}" for note in brief.open_questions
                                    )
                                    or "(none)",
                                    turns="\n\n".join(
                                        f"User: {_clip(turn.request)}\n"
                                        f"Answer ({turn.status}): {_clip(turn.answer)}"
                                        for turn in turns
                                    ),
                                )
                            ),
                        ),
                        temperature=0.0,
                        response_format={"type": "json_object"},
                    )
                )
                parsed = extract_object(response.content) or {}
                summary = " ".join(str(parsed.get("stage_summary", "")).split())
                if summary:
                    open_texts = {note.text for note in brief.open_questions}
                    raw = parsed.get("resolved_questions") or ()
                    resolved = tuple(
                        text
                        for item in (raw if isinstance(raw, list | tuple) else ())
                        if (text := str(item).strip()) in open_texts
                    )
                    goal = " ".join(str(parsed.get("goal_brief", "")).split())
                    return summary[:1200], goal[:900], resolved, True
                log.warning("session.compaction_unreadable")
            except Exception as error:
                log.warning("session.summarisation_failed", error=str(error))
        fallback = " | ".join(
            f"{_clip(turn.request, 120)} -> {_clip(turn.answer, 160)}" for turn in turns
        )
        return fallback[:1200], "", (), False

    async def _artifacts(self, thread: Sequence[Objective]) -> dict[UUID, list[str]]:
        if self._plans is None or self._tool_calls is None:
            return {}
        found: dict[UUID, list[str]] = {}
        for objective in thread:
            if objective.result is None or not objective.result.output.get("delegated"):
                continue
            try:
                calls = []
                for plan in await self._plans.for_objective(objective.id):
                    for task in plan.tasks:
                        calls.extend(await self._tool_calls.list_for_task(task.id))
                files = produced_files(calls)
            except Exception as error:
                log.warning("session.artifacts_unreadable", error=str(error))
                continue
            if files:
                found[objective.id] = files
        return found

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """Folding a handful of turns is summarising, paid once per stage.

        SYNTHESIS rather than EXTRACTION: a stage summary is what a long thread
        remembers of six requests, and a cheap model that drops the one decision
        that mattered loses it for the rest of the thread.
        """
        return (
            TaskKind.SYNTHESIS,
            CapabilityRequirement(),
            RoutingHints(quality=0.6, cost_sensitivity=0.6),
        )


def _clip(text: str, limit: int = TURN_CHARS) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"
