"""Memory on a real SQLite file, with the real index behind it.

The unit tests run against the in-memory store, which shares the domain's
ranking and access rules but not its search. What is checked here is the half
that is SQLite's: that FTS5 exists where the migration says it does, that the
triggers keep it in step with the table, and that prose full of punctuation is a
query rather than a syntax error.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.memory.models import MemoryItem, MemoryKind, MemoryQuery, MemoryScope
from infrastructure.memory.sql import SqlMemory

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def memory(session_factory: async_sessionmaker[AsyncSession]) -> SqlMemory:
    return SqlMemory(session_factory)


def item(content: str, **extra) -> MemoryItem:
    extra.setdefault("scope", MemoryScope.WORKSPACE)
    extra.setdefault("kind", MemoryKind.EPISODIC)
    return MemoryItem.create(content, scope=extra.pop("scope"), kind=extra.pop("kind"), **extra)


async def test_what_was_remembered_is_found_by_the_words_in_it(memory: SqlMemory) -> None:
    await memory.remember(item("The quarterly report lives in reports/q3.md"))
    await memory.remember(item("The invoices live in finance/2026"))

    found = await memory.recall(MemoryQuery(text="Where is the quarterly report?"))

    # The index is generous - it matches word by word, so an item sharing only
    # "the" and "in" is a hit - and the ranking is what makes that usable. What
    # is asked for comes first; what merely shares a word does not.
    assert found[0].content == "The quarterly report lives in reports/q3.md"


async def test_prose_with_punctuation_is_a_query_not_a_syntax_error(
    memory: SqlMemory,
) -> None:
    """A goal is prose, and FTS5's query language is full of operators.

    "NEAR", a colon, an unbalanced quote - any one of them turns a search into
    an exception if the text is passed through untouched.
    """
    await memory.remember(item("The build fails on macOS with a linker error"))

    found = await memory.recall(
        MemoryQuery(text='NOT "macOS": why does the build fail (linker)? OR* AND')
    )

    assert len(found) == 1


async def test_a_search_that_matches_nothing_returns_nothing(memory: SqlMemory) -> None:
    await memory.remember(item("Something entirely unrelated"))

    assert await memory.recall(MemoryQuery(text="kangaroo")) == []


async def test_rewriting_an_item_rewrites_what_is_searchable(memory: SqlMemory) -> None:
    """The index is kept in step by triggers, so this is the trigger's test."""
    original = item("The report is in reports/q2.md")
    await memory.remember(original)
    await memory.remember(replace(original, content="The report moved to reports/q3.md"))

    assert await memory.recall(MemoryQuery(text="q2")) == []
    assert len(await memory.recall(MemoryQuery(text="q3"))) == 1
    assert len(await memory.recall(MemoryQuery(limit=50))) == 1, "one item, not two"


async def test_forgetting_removes_it_from_the_index_too(memory: SqlMemory) -> None:
    remembered = item("A thing that will be forgotten")
    await memory.remember(remembered)

    assert await memory.forget([remembered.id]) == 1
    assert await memory.recall(MemoryQuery(text="forgotten")) == []


async def test_forgetting_within_a_query_reaches_only_what_it_could_read(
    memory: SqlMemory,
) -> None:
    """A person forgets a line on their screen, not an id from somewhere else."""
    here = item("Here: the invoices are in finance/2026")
    elsewhere = item("Elsewhere: the invoices are in old/", workspace_id="other")
    private = item(
        "Private: the key is in the vault",
        scope=MemoryScope.EMPLOYEE_PRIVATE,
        kind=MemoryKind.SEMANTIC,
        employee_id=uuid4(),
    )
    for one in (here, elsewhere, private):
        await memory.remember(one)
    shown = MemoryQuery(scopes=frozenset({MemoryScope.WORKSPACE, MemoryScope.USER}))

    assert await memory.forget([elsewhere.id, private.id], within=shown) == 0
    assert await memory.forget([here.id, elsewhere.id], within=shown) == 1

    assert await memory.recall(MemoryQuery(text="invoices")) == []
    assert await memory.recall(
        MemoryQuery(text="invoices", workspace_id="other")
    ) == [elsewhere]


async def test_an_employee_never_reads_another_employees_private_memory(
    memory: SqlMemory,
) -> None:
    """The isolation rule (§9.8), against the store that actually holds rows."""
    mine, theirs = uuid4(), uuid4()
    await memory.remember(
        item("Mine: the key is in the vault", scope=MemoryScope.EMPLOYEE_PRIVATE,
             kind=MemoryKind.SEMANTIC, employee_id=mine)
    )
    await memory.remember(
        item("Theirs: the key is under the mat", scope=MemoryScope.EMPLOYEE_PRIVATE,
             kind=MemoryKind.SEMANTIC, employee_id=theirs)
    )

    found = await memory.recall(
        MemoryQuery(
            text="where is the key",
            scopes=frozenset({MemoryScope.WORKSPACE, MemoryScope.EMPLOYEE_PRIVATE}),
            employee_id=mine,
        )
    )

    assert [i.content for i in found] == ["Mine: the key is in the vault"]


async def test_expiry_is_read_on_recall_and_enforced_on_prune(memory: SqlMemory) -> None:
    await memory.remember(
        item("Yesterday's note", kind=MemoryKind.WORKING, expires_at=NOW - timedelta(hours=1))
    )
    await memory.remember(item("Still true", kind=MemoryKind.SEMANTIC))

    live = await memory.recall(MemoryQuery(as_of=NOW))
    assert [i.content for i in live] == ["Still true"]

    assert await memory.prune(now=NOW) == 1
    assert await memory.prune(now=NOW) == 0


async def test_what_matters_outranks_what_merely_matched(memory: SqlMemory) -> None:
    await memory.remember(
        item("The report is somewhere", importance=0.2, created_at=NOW - timedelta(days=90))
    )
    await memory.remember(
        item("The report is in reports/q3.md", kind=MemoryKind.SEMANTIC, importance=0.9,
             created_at=NOW - timedelta(days=1))
    )

    found = await memory.recall(MemoryQuery(text="where is the report", as_of=NOW))

    assert found[0].content == "The report is in reports/q3.md"


async def test_memory_survives_the_process_that_wrote_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await SqlMemory(session_factory).remember(item("Written by the first process"))

    # A second adapter over the same file is the closest a test gets to a
    # restart, which is the point of memory in the first place.
    found = await SqlMemory(session_factory).recall(MemoryQuery(text="first process"))

    assert len(found) == 1
    assert found[0].created_at.tzinfo is not None


async def test_metadata_and_importance_come_back_as_they_went_in(
    memory: SqlMemory,
) -> None:
    stored = item("With provenance", metadata={"employee": "organizer"}, importance=0.75)
    await memory.remember(stored)

    [found] = await memory.recall(MemoryQuery(text="provenance"))

    assert found.metadata == {"employee": "organizer"}
    assert found.importance == pytest.approx(0.75)
    assert found.id == stored.id
