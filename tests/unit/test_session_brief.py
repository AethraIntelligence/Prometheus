"""A long thread keeps what it established, in a context that does not grow (Phase 9).

The Definition of Done for the phase, as tests: a thread of many requests still
reaches the manager inside a fixed budget, a decision made early is still in
front of it, every file the thread wrote is indexed, and compacting the history
never touches the history itself. A restart in the middle loses nothing and
folds nothing twice.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from application.conversations.session import SessionMemory
from domain.conversations import session as rules
from domain.conversations.session import SessionState
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.objective_repository import InMemoryObjectiveRepository
from infrastructure.persistence.session_repository import InMemorySessionStateRepository
from tests.fakes.llm import FakeLLM, reply, transient
from tests.unit.test_prometheus_manager import build, intent

START = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def turn(
    conversation_id,
    index: int,
    *,
    text: str = "",
    answer: str = "",
    constraints: dict | None = None,
    status: ObjectiveStatus = ObjectiveStatus.DONE,
    missing: tuple[str, ...] = (),
) -> Objective:
    objective = Objective.create(
        text or f"Request number {index}",
        conversation_id=conversation_id,
        constraints=constraints or {},
        created_at=START + timedelta(minutes=index),
    )
    return replace(
        objective,
        status=status,
        finished_at=START + timedelta(minutes=index, seconds=30),
        result=ObjectiveResult(
            objective_id=objective.id,
            summary=answer or f"Answer number {index}",
            status=status,
            missing=missing,
        ),
    )


def thread(n: int, **overrides):
    conversation_id = uuid4()
    return conversation_id, [turn(conversation_id, i, **overrides) for i in range(n)]


# --- Projection ----------------------------------------------------------------


def test_decisions_questions_and_files_are_read_off_the_record() -> None:
    conversation_id = uuid4()
    first = turn(conversation_id, 0, constraints={"format": "CSV", "folder": "sales"})
    second = turn(
        conversation_id,
        1,
        constraints={"format": "XLSX"},
        status=ObjectiveStatus.ESCALATED,
        missing=("the Q3 totals",),
    )
    current = turn(conversation_id, 2)

    brief = rules.project(
        None,
        conversation_id,
        [first, second, current],
        {first.id: ["sales/summary.csv"], second.id: ["sales/summary.xlsx"]},
        exclude=current.id,
    )

    assert [note.text for note in brief.decisions] == ["folder: sales", "format: XLSX"], (
        "a later request's decision replaces an earlier one on the same point"
    )
    assert [note.text for note in brief.open_questions] == ["the Q3 totals"]
    assert brief.open_questions[0].objective_id == second.id
    assert [item.path for item in brief.artifacts] == ["sales/summary.csv", "sales/summary.xlsx"]
    assert brief.total_turns == 2, "the request being worked on is not its own history"
    assert brief.goal == first.text


def test_a_settled_question_is_no_longer_open() -> None:
    conversation_id = uuid4()
    escalated = turn(
        conversation_id, 0, status=ObjectiveStatus.ESCALATED, missing=("the Q3 totals",)
    )
    state = rules.resolving(None, conversation_id, "The Q3 totals")

    assert rules.project(state, conversation_id, [escalated]).open_questions == ()


# --- When to compact -------------------------------------------------------------


def test_nothing_is_compacted_until_a_whole_stage_sits_outside_the_recent_window() -> None:
    _, short = thread(rules.RECENT_TURNS + rules.STAGE_SIZE - 1)
    _, enough = thread(rules.RECENT_TURNS + rules.STAGE_SIZE)

    assert rules.due_for_compaction(None, short) == []
    folded = rules.due_for_compaction(None, enough)
    assert folded == enough[: rules.STAGE_SIZE], "the oldest turns, never the recent ones"


def test_a_turn_is_folded_once_however_often_it_is_offered() -> None:
    conversation_id, turns = thread(12)
    state = rules.with_stage(None, conversation_id, turns[:6], summary="first six")

    again = rules.with_stage(state, conversation_id, turns[:6], summary="first six, again")

    assert again == state
    assert rules.due_for_compaction(state, turns) == []


# --- The budget ------------------------------------------------------------------


def test_a_very_long_thread_stays_inside_the_budget_and_keeps_its_first_decision() -> None:
    conversation_id = uuid4()
    turns = [
        turn(
            conversation_id,
            i,
            answer="x" * 5_000,
            constraints={"output_format": "CSV"} if i == 2 else {},
        )
        for i in range(300)
    ]
    state = None
    while folded := rules.due_for_compaction(state, turns):
        state = rules.with_stage(state, conversation_id, folded, summary="s" * 2_000)

    lines = rules.render(rules.project(state, conversation_id, turns), budget=12_000)

    assert sum(len(line) for line in lines) <= 12_000
    assert "output_format: CSV" in lines[0]
    assert "300 earlier request(s)" in lines[0]
    assert "older stage(s) are covered by the goal" in lines[0]


# --- Compaction with a store and a model -----------------------------------------


async def seeded(n: int, **overrides):
    objectives = InMemoryObjectiveRepository()
    conversation_id, turns = thread(n, **overrides)
    for item in turns:
        await objectives.save(item)
    return objectives, conversation_id, turns


def compaction(goal: str = "", summary: str = "", resolved=()) -> str:
    return json.dumps(
        {"goal_brief": goal, "stage_summary": summary, "resolved_questions": list(resolved)}
    )


async def test_a_stage_is_summarised_and_the_history_is_left_as_it_was() -> None:
    objectives, conversation_id, turns = await seeded(12)
    before = await objectives.for_conversation(conversation_id)
    states = InMemorySessionStateRepository()
    llm = FakeLLM([reply(compaction("Build the Q3 report", "Chose CSV; wrote q3.csv"))])
    sessions = SessionMemory(objectives=objectives, states=states, llm=llm)

    state = await sessions.after(turns[-1])

    assert state is not None and len(state.stages) == 1
    assert state.stages[0].summary == "Chose CSV; wrote q3.csv"
    assert state.stages[0].summarised
    assert state.goal_brief == "Build the Q3 report"
    assert await objectives.for_conversation(conversation_id) == before, (
        "compaction shortens what a model is shown, not what happened"
    )


async def test_an_unreachable_summariser_still_compacts_with_the_fallback() -> None:
    objectives, _, turns = await seeded(12)
    sessions = SessionMemory(
        objectives=objectives,
        states=InMemorySessionStateRepository(),
        llm=FakeLLM([transient()]),
    )

    state = await sessions.after(turns[-1])

    assert state is not None and len(state.stages) == 1
    assert not state.stages[0].summarised
    assert "Request number 0" in state.stages[0].summary


async def test_a_restart_continues_where_the_last_process_stopped() -> None:
    objectives, conversation_id, turns = await seeded(18)
    states = InMemorySessionStateRepository()
    # The first process folds one stage and dies before the next is due.
    await states.save(
        rules.with_stage(None, conversation_id, turns[:6], summary="stage one")
    )

    fresh = SessionMemory(
        objectives=objectives,
        states=states,
        llm=FakeLLM([reply(compaction(summary="stage two"))]),
    )
    state = await fresh.after(turns[-1])

    assert state is not None
    assert [stage.summary for stage in state.stages] == ["stage one", "stage two"]
    assert state.stages[1].objective_ids == tuple(item.id for item in turns[6:12])


async def test_a_resolved_question_must_be_one_that_was_open() -> None:
    objectives, _, turns = await seeded(
        12, status=ObjectiveStatus.ESCALATED, missing=("the totals",)
    )
    llm = FakeLLM(
        [reply(compaction(summary="done", resolved=["the totals", "something invented"]))]
    )
    sessions = SessionMemory(
        objectives=objectives, states=InMemorySessionStateRepository(), llm=llm
    )

    state = await sessions.after(turns[-1])

    assert state is not None
    assert state.resolved_questions == ("the totals",)


async def test_a_person_can_only_resolve_an_open_question_in_its_workspace() -> None:
    objectives, conversation_id, turns = await seeded(
        1, status=ObjectiveStatus.ESCALATED, missing=("the totals",)
    )
    workspace_id = WorkspaceId("client-project")
    turns[0] = replace(turns[0], workspace_id=workspace_id)
    await objectives.save(turns[0])
    states = InMemorySessionStateRepository()
    sessions = SessionMemory(objectives=objectives, states=states)

    try:
        await sessions.resolve(conversation_id, "something never asked")
    except ValueError as error:
        assert str(error) == "That question is not open in this thread."
    else:
        raise AssertionError("a non-existent question was marked resolved")

    brief = await sessions.resolve(conversation_id, "The Totals")
    state = await states.get(conversation_id)
    assert brief.open_questions == ()
    assert state is not None and state.workspace_id == workspace_id


# --- Through the manager ---------------------------------------------------------


async def test_the_manager_reads_a_long_thread_as_a_brief_within_budget() -> None:
    objectives = InMemoryObjectiveRepository()
    conversation_id = uuid4()
    for i in range(40):
        await objectives.save(
            turn(
                conversation_id,
                i,
                answer="y" * 3_000,
                constraints={"delivery": "email to finance"} if i == 1 else {},
            )
        )
    states = InMemorySessionStateRepository()
    sessions = SessionMemory(objectives=objectives, states=states, budget=8_000)
    llm = FakeLLM(
        [reply(intent(needs_work=False, answer="Sure.", acceptance_criteria=[]))]
    )
    manager, _, _, _ = build(script=[], objectives=objectives, session=sessions, llm=llm)

    objective = await manager.receive("And the same for October?", conversation_id=conversation_id)
    await manager.handle_objective(objective)

    prompt = llm.requests[0].messages[-1].content
    assert "delivery: email to finance" in prompt
    assert "40 earlier request(s)" in prompt
    assert prompt.count("y" * 3_000) <= 2, "the budget holds however long the thread is"
    stored = await states.get(conversation_id)
    assert isinstance(stored, SessionState) and stored.stages, (
        "answering the request compacted the thread behind it"
    )
