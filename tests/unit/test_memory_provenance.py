"""Every memory says what it rests on, and a newer one can replace it (Phase 9).

Three promises, each a test here. A recollection reaches a model labelled as
what it is - stated, recorded, reported or inferred - so a guess is not read as
a fact. A memory that is replaced is superseded rather than erased, and a run
never reads it again. And where two memories disagree and nothing settles which
is right, both are kept, both are marked, and neither silently wins.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from application.memory.consolidation import Consolidator
from application.memory.recorder import MemoryRecorder
from application.memory.revision import MemoryReviser
from domain.memory.access import visible
from domain.memory.citation import cite, explain, recollection
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
from domain.memory.ranking import SUPERSEDED_RETENTION, score
from domain.memory.revision import Relation, revise
from domain.tasks.task import Task, TaskResult, TaskStatus
from infrastructure.memory.in_memory import InMemoryMemory
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply, transient

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def note(content: str, **extra) -> MemoryItem:
    extra.setdefault("scope", MemoryScope.USER)
    extra.setdefault("kind", MemoryKind.SEMANTIC)
    return MemoryItem.create(content, scope=extra.pop("scope"), kind=extra.pop("kind"), **extra)


# --- The rule for disagreement -------------------------------------------------


def test_a_replacement_supersedes_and_keeps_the_old_memory_on_record() -> None:
    old = note("The user prefers: Markdown", basis=MemoryBasis.STATED)
    new = note("The user prefers: plain text", basis=MemoryBasis.STATED)

    new_after, old_after = revise(new, old, Relation.REPLACES, now=NOW)

    assert new_after.status is MemoryStatus.ACTIVE
    assert old_after.status is MemoryStatus.SUPERSEDED
    assert old_after.superseded_by == new.id
    assert old_after.revised_at == NOW
    assert old_after.content == old.content, "superseding is not rewriting"


def test_weaker_evidence_does_not_overrule_stronger_it_contests_it() -> None:
    stated = note("Reports go to reports/", basis=MemoryBasis.STATED)
    guessed = note("Reports go to archive/", basis=MemoryBasis.INFERRED)

    new_after, old_after = revise(guessed, stated, Relation.CONTRADICTS, now=NOW)

    assert new_after.status is MemoryStatus.CONTESTED
    assert old_after.status is MemoryStatus.CONTESTED
    assert new_after.contradicts == (stated.id,)
    assert old_after.contradicts == (guessed.id,)


def test_stronger_evidence_supersedes_what_it_contradicts() -> None:
    guessed = note("The backup runs at 01:00", basis=MemoryBasis.INFERRED)
    recorded = note("The backup runs at 03:00", basis=MemoryBasis.OBSERVED)

    _, old_after = revise(recorded, guessed, Relation.CONTRADICTS)

    assert old_after.status is MemoryStatus.SUPERSEDED


def test_unrelated_memories_are_left_alone() -> None:
    a, b = note("one"), note("two")
    assert revise(a, b, Relation.UNRELATED) == (a, b)


# --- Reading -------------------------------------------------------------------


def test_a_superseded_memory_is_not_read_unless_a_person_asks_for_history() -> None:
    item = replace(note("old"), status=MemoryStatus.SUPERSEDED)
    query = MemoryQuery(scopes=frozenset({MemoryScope.USER}))

    assert not visible(item, query)
    assert visible(item, replace(query, include_superseded=True))


def test_history_does_not_resurrect_an_expired_memory() -> None:
    item = replace(
        note("expired"),
        status=MemoryStatus.SUPERSEDED,
        expires_at=NOW - timedelta(seconds=1),
    )
    query = MemoryQuery(
        scopes=frozenset({MemoryScope.USER}),
        include_superseded=True,
        as_of=NOW,
    )

    assert not visible(item, query)


def test_a_fact_outranks_an_assumption_that_matches_as_well() -> None:
    fact = note("x", basis=MemoryBasis.STATED, confidence=1.0, created_at=NOW)
    guess = note("x", basis=MemoryBasis.INFERRED, confidence=0.3, created_at=NOW)
    contested = replace(fact, status=MemoryStatus.CONTESTED)

    assert score(fact, now=NOW) > score(guess, now=NOW) > 0
    assert score(contested, now=NOW) < score(fact, now=NOW)


def test_a_recollection_carries_basis_source_time_confidence_and_scope() -> None:
    item = note(
        "Invoices are in finance/2026",
        scope=MemoryScope.WORKSPACE,
        basis=MemoryBasis.REPORTED,
        confidence=0.75,
        created_at=NOW,
        provenance=Provenance(kind=SourceKind.TASK, ref="t-1", label="Sort the invoices"),
    )

    line = recollection(item)

    assert line.startswith("Invoices are in finance/2026 [")
    for part in (
        "reported by an employee",
        'a task "Sort the invoices"',
        "2026-09-17",
        "confidence 0.75",
        "this workspace",
    ):
        assert part in line
    assert "disputed" in cite(replace(item, status=MemoryStatus.CONTESTED))


def test_why_a_memory_was_chosen_is_stated_from_the_record() -> None:
    item = note("The quarterly report lives in reports/q3.md", created_at=NOW - timedelta(days=3))

    why = explain(item, MemoryQuery(text="Where is the quarterly report?"), now=NOW)

    assert why.matched == ("quarterly", "report")
    assert "the user, in every workspace" in why.reason
    assert "3 day(s)" in why.reason


# --- Writing -------------------------------------------------------------------


async def test_every_memory_a_task_leaves_names_the_task_and_what_it_rests_on() -> None:
    memory = InMemoryMemory()
    task = replace(
        replace(Task.create("Sort the invoices"), plan_id=uuid4()),
        status=TaskStatus.COMPLETED,
        result=TaskResult(
            summary="Moved 3 invoices to finance/2026",
            output={"observations": [{"details": {"tool": "fs.move"}, "succeeded": True}]},
        ),
    )
    employee = definition("organizer")

    await MemoryRecorder(memory).record_task(task, employee)

    stored = await memory.recall(
        MemoryQuery(
            scopes=frozenset(MemoryScope),
            employee_id=employee.id,
            plan_id=task.plan_id,
            limit=10,
        )
    )
    assert len(stored) == 3
    assert all(item.provenance.kind is SourceKind.TASK for item in stored)
    assert all(item.provenance.ref == str(task.id) for item in stored)
    by_kind = {item.kind: item for item in stored if item.scope is not MemoryScope.PLAN}
    assert by_kind[MemoryKind.EPISODIC].basis is MemoryBasis.REPORTED
    assert by_kind[MemoryKind.SEMANTIC].basis is MemoryBasis.OBSERVED


async def test_a_preference_names_the_request_it_was_read_from() -> None:
    memory = InMemoryMemory()
    objective_id = str(uuid4())

    await MemoryRecorder(memory).record_preferences(
        ["answers in Markdown"], source="Always answer in Markdown", objective_id=objective_id
    )

    (item,) = await memory.recall(MemoryQuery(scopes=frozenset({MemoryScope.USER})))
    assert item.basis is MemoryBasis.STATED
    assert item.provenance.kind is SourceKind.OBJECTIVE
    assert item.provenance.ref == objective_id
    assert item.provenance.label == "Always answer in Markdown"


async def test_a_summary_is_an_assumption_that_names_what_it_replaced() -> None:
    memory = InMemoryMemory()
    for index in range(3):
        await memory.remember(
            note(
                f"Sorted batch {index}",
                scope=MemoryScope.WORKSPACE,
                kind=MemoryKind.EPISODIC,
                basis=MemoryBasis.REPORTED,
                confidence=0.75,
                provenance=Provenance(kind=SourceKind.TASK, ref=f"task-{index}"),
            )
        )

    folded = await Consolidator(
        FakeLLM([reply("Batches are sorted weekly.")]), memory, memory, threshold=3, batch=3
    ).consolidate()

    assert folded is not None
    assert folded.basis is MemoryBasis.INFERRED
    assert folded.confidence < 0.75
    assert folded.provenance.kind is SourceKind.CONSOLIDATION
    assert set(folded.provenance.derived_from) == {"task-0", "task-1", "task-2"}


async def test_a_changed_preference_supersedes_the_old_one() -> None:
    memory = InMemoryMemory()
    old = note("The user prefers: answers in Markdown", basis=MemoryBasis.STATED)
    await memory.remember(old)
    reviser = MemoryReviser(FakeLLM([reply('{"replaces": [1], "contradicts": []}')]), memory)

    stored = await reviser.remember(
        note("The user prefers: answers in plain text, not Markdown", basis=MemoryBasis.STATED)
    )

    current = await memory.recall(MemoryQuery(scopes=frozenset({MemoryScope.USER})))
    assert [item.id for item in current] == [stored.id]
    history = await memory.recall(
        MemoryQuery(scopes=frozenset({MemoryScope.USER}), include_superseded=True)
    )
    (previous,) = [item for item in history if item.id == old.id]
    assert previous.status is MemoryStatus.SUPERSEDED
    assert previous.superseded_by == stored.id


async def test_a_workspace_note_does_not_supersede_a_global_preference() -> None:
    memory = InMemoryMemory()
    global_preference = note(
        "Reports use Markdown",
        scope=MemoryScope.USER,
        basis=MemoryBasis.STATED,
    )
    await memory.remember(global_preference)
    llm = FakeLLM([reply('{"replaces": [1], "contradicts": []}')])
    reviser = MemoryReviser(llm, memory)

    local_note = await reviser.remember(
        note(
            "Reports use plain text in this workspace",
            scope=MemoryScope.WORKSPACE,
            basis=MemoryBasis.STATED,
        )
    )

    global_items = await memory.recall(
        MemoryQuery(scopes=frozenset({MemoryScope.USER}))
    )
    local_items = await memory.recall(
        MemoryQuery(scopes=frozenset({MemoryScope.WORKSPACE}))
    )
    assert [item.id for item in global_items] == [global_preference.id]
    assert [item.id for item in local_items] == [local_note.id]
    assert not llm.requests, "different scopes are not revision candidates"


async def test_a_judge_that_cannot_be_read_or_reached_changes_nothing_but_the_new_line() -> None:
    for answer in (reply("I think they are related."), transient()):
        memory = InMemoryMemory()
        old = note("The user prefers: Markdown answers")
        await memory.remember(old)

        stored = await MemoryReviser(FakeLLM([answer]), memory).remember(
            note("The user prefers: Markdown tables")
        )

        current = await memory.recall(MemoryQuery(scopes=frozenset({MemoryScope.USER})))
        assert {item.id for item in current} == {old.id, stored.id}
        assert all(item.status is MemoryStatus.ACTIVE for item in current)


async def test_a_person_correcting_a_memory_supersedes_it_without_asking_a_model() -> None:
    old = note("Reports go to reports/2025", basis=MemoryBasis.REPORTED, confidence=0.7)

    new, old_after = MemoryReviser.correction(old, "Reports go to reports/2026")

    assert new.basis is MemoryBasis.STATED and new.confidence == 1.0
    assert new.provenance.kind is SourceKind.PERSON
    assert new.provenance.derived_from == (str(old.id),)
    assert old_after.status is MemoryStatus.SUPERSEDED


async def test_a_superseded_memory_is_pruned_once_its_retention_is_over() -> None:
    memory = InMemoryMemory()
    kept = replace(note("recent"), status=MemoryStatus.SUPERSEDED, revised_at=NOW)
    due = replace(
        note("old"),
        status=MemoryStatus.SUPERSEDED,
        revised_at=NOW - SUPERSEDED_RETENTION - timedelta(days=1),
    )
    for item in (kept, due, note("current")):
        await memory.remember(item)

    assert await memory.prune(now=NOW) == 1
    history = await memory.recall(
        MemoryQuery(scopes=frozenset({MemoryScope.USER}), include_superseded=True, as_of=NOW)
    )
    assert due.id not in {item.id for item in history}
