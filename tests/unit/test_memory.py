"""Memory: what comes back, in what order, and what never comes back at all."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from application.memory.consolidation import Consolidator
from application.memory.recorder import MemoryRecorder
from domain.memory.models import MemoryItem, MemoryKind, MemoryQuery, MemoryScope
from domain.memory.ranking import decay, expires_at, is_live, score
from domain.tasks.plan import Observation
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workspace.models import WorkspaceId
from infrastructure.memory.in_memory import InMemoryMemory
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def item(content: str, **extra) -> MemoryItem:
    extra.setdefault("scope", MemoryScope.WORKSPACE)
    extra.setdefault("kind", MemoryKind.EPISODIC)
    scope = extra.pop("scope")
    kind = extra.pop("kind")
    return MemoryItem.create(content, scope=scope, kind=kind, **extra)


# --- Scope is an access boundary (§9.8) ---------------------------------------


async def test_an_employee_never_reads_another_employees_private_memory() -> None:
    """The isolation test the phase exists to make possible.

    Not a matter of what the query asks for: the store is asked for private
    memory *by* one employee and answers with that employee's, whatever else it
    holds.
    """
    memory = InMemoryMemory()
    mine, theirs = uuid4(), uuid4()
    await memory.remember(
        item("The invoices live in finance/2026", scope=MemoryScope.EMPLOYEE_PRIVATE,
             kind=MemoryKind.SEMANTIC, employee_id=mine)
    )
    await memory.remember(
        item("The invoices live somewhere else entirely", scope=MemoryScope.EMPLOYEE_PRIVATE,
             kind=MemoryKind.SEMANTIC, employee_id=theirs)
    )

    recalled = await memory.recall(
        MemoryQuery(
            text="invoices",
            scopes=frozenset({MemoryScope.EMPLOYEE_PRIVATE}),
            employee_id=mine,
        )
    )

    assert [found.content for found in recalled] == ["The invoices live in finance/2026"]


async def test_naming_no_employee_reads_no_private_memory_at_all() -> None:
    memory = InMemoryMemory()
    await memory.remember(
        item("Private", scope=MemoryScope.EMPLOYEE_PRIVATE, kind=MemoryKind.SEMANTIC,
             employee_id=uuid4())
    )

    everything = await memory.recall(
        MemoryQuery(scopes=frozenset(MemoryScope), limit=50)
    )

    assert everything == []


async def test_one_plans_memory_is_not_another_plans() -> None:
    memory = InMemoryMemory()
    mine, theirs = uuid4(), uuid4()
    await memory.remember(item("Ours", scope=MemoryScope.PLAN, plan_id=mine))
    await memory.remember(item("Theirs", scope=MemoryScope.PLAN, plan_id=theirs))

    recalled = await memory.recall(
        MemoryQuery(scopes=frozenset({MemoryScope.PLAN}), plan_id=mine)
    )

    assert [found.content for found in recalled] == ["Ours"]


async def test_another_workspaces_memory_is_invisible() -> None:
    from domain.workspace.models import WorkspaceId

    memory = InMemoryMemory()
    await memory.remember(item("Elsewhere", workspace_id=WorkspaceId("other")))

    assert await memory.recall(MemoryQuery()) == []


# --- Ranking and growth (§9.7) ------------------------------------------------


async def test_forgetting_within_a_query_leaves_what_it_could_not_read() -> None:
    store = InMemoryMemory()
    here = item("here")
    private = item(
        "private", scope=MemoryScope.EMPLOYEE_PRIVATE, employee_id=uuid4()
    )
    elsewhere = item("elsewhere", workspace_id=WorkspaceId("other"))
    for one in (here, private, elsewhere):
        await store.remember(one)
    shown = MemoryQuery(scopes=frozenset({MemoryScope.WORKSPACE, MemoryScope.USER}))

    assert await store.forget([here.id, private.id, elsewhere.id], within=shown) == 1
    assert await store.forget([private.id, elsewhere.id]) == 2


def test_what_matters_and_what_is_recent_outrank_what_is_neither() -> None:
    old_and_important = item("A", kind=MemoryKind.SEMANTIC, importance=0.9,
                             created_at=NOW - timedelta(days=60))
    recent_and_trivial = item("B", kind=MemoryKind.EPISODIC, importance=0.2,
                              created_at=NOW - timedelta(hours=1))
    old_and_trivial = item("C", kind=MemoryKind.EPISODIC, importance=0.2,
                           created_at=NOW - timedelta(days=60))

    ranked = sorted(
        (old_and_important, recent_and_trivial, old_and_trivial),
        key=lambda i: -score(i, now=NOW),
    )

    assert [i.content for i in ranked] == ["A", "B", "C"]


def test_a_working_note_fades_far_faster_than_a_preference() -> None:
    assert decay(MemoryKind.WORKING, age_days=1.0) < 0.1
    assert decay(MemoryKind.SEMANTIC, age_days=1.0) > 0.99


def test_working_memory_expires_and_a_preference_does_not() -> None:
    assert expires_at(MemoryKind.WORKING, NOW) == NOW + timedelta(hours=12)
    assert expires_at(MemoryKind.SEMANTIC, NOW) is None


async def test_an_expired_item_is_neither_recalled_nor_kept() -> None:
    memory = InMemoryMemory()
    await memory.remember(
        item("Yesterday's scratch note", kind=MemoryKind.WORKING,
             expires_at=NOW - timedelta(hours=1))
    )
    await memory.remember(item("Still true", kind=MemoryKind.SEMANTIC))

    live = await memory.recall(MemoryQuery(as_of=NOW, kinds=frozenset()))
    assert [found.content for found in live] == ["Still true"]

    assert await memory.prune(now=NOW) == 1
    assert await memory.prune(now=NOW) == 0


def test_expiry_is_read_as_of_the_moment_asked_about() -> None:
    note = item("x", kind=MemoryKind.WORKING, expires_at=NOW)
    assert is_live(note, NOW - timedelta(minutes=1))
    assert not is_live(note, NOW + timedelta(minutes=1))


# --- Searching ----------------------------------------------------------------


async def test_a_search_that_matches_nothing_returns_nothing() -> None:
    memory = InMemoryMemory()
    await memory.remember(item("The quarterly report is in reports/q3.md"))

    assert await memory.recall(MemoryQuery(text="dentist appointment")) == []
    assert len(await memory.recall(MemoryQuery(text="where is the quarterly report"))) == 1


async def test_an_empty_query_lists_rather_than_searches() -> None:
    memory = InMemoryMemory()
    await memory.remember(item("Anything at all"))

    assert len(await memory.recall(MemoryQuery())) == 1


# --- Writing (§9.3, §9.5) -----------------------------------------------------


async def test_a_finished_task_leaves_the_workspace_and_the_employee_something() -> None:
    memory = InMemoryMemory()
    employee = definition("organizer")
    task = Task.create("Sort the invoices folder")
    task, _ = task.transition_to(TaskStatus.PLANNING)
    task, _ = task.transition_to(TaskStatus.RUNNING)
    task, _ = task.transition_to(TaskStatus.VERIFYING)
    task, _ = task.transition_to(
        TaskStatus.COMPLETED,
        result=TaskResult(
            summary="Moved 12 invoices into finance/2026.",
            output={"observations": [
                Observation(step=1, summary="fs.list returned 12",
                            details={"tool": "fs.list"}).to_dict(),
                Observation(step=2, summary="web.search failed", succeeded=False,
                            details={"tool": "web.search"}).to_dict(),
            ]},
        ),
    )

    await MemoryRecorder(memory).record_task(task, employee)

    workspace = await memory.recall(MemoryQuery(text="invoices"))
    assert any("finance/2026" in found.content for found in workspace)

    private = await memory.recall(
        MemoryQuery(
            text="invoices",
            scopes=frozenset({MemoryScope.EMPLOYEE_PRIVATE}),
            employee_id=employee.id,
        )
    )
    lesson = " ".join(found.content for found in private)
    assert "fs.list" in lesson and "web.search" in lesson


async def test_a_failure_is_remembered_too_and_matters_less() -> None:
    memory = InMemoryMemory()
    task = Task.create("Do the impossible")
    task, _ = task.transition_to(TaskStatus.PLANNING)
    task, _ = task.transition_to(TaskStatus.RUNNING)
    task, _ = task.transition_to(TaskStatus.VERIFYING)
    done, _ = task.transition_to(TaskStatus.COMPLETED, result=TaskResult(summary="Did it."))
    failed, _ = task.transition_to(
        TaskStatus.FAILED, result=TaskResult(summary="Could not reach the site.")
    )

    await MemoryRecorder(memory).record_task(failed, definition())
    remembered = await memory.recall(MemoryQuery(text="impossible"))

    assert remembered and "FAILED" in remembered[0].content
    assert remembered[0].content.startswith("Could not reach the site."), (
        "what was found out comes before what was asked for"
    )
    assert remembered[0].importance < 0.7  # what a success would have been worth
    assert done.status is TaskStatus.COMPLETED  # the two states are distinct records


async def test_a_preference_is_stored_one_per_item_and_never_expires() -> None:
    memory = InMemoryMemory()

    await MemoryRecorder(memory).record_preferences(
        ("always answer in Markdown", "never touch the originals", "  "),
        source="the request",
    )

    stored = await memory.recall(
        MemoryQuery(
            kinds=frozenset({MemoryKind.SEMANTIC}),
            scopes=frozenset({MemoryScope.USER}),
        )
    )
    assert len(stored) == 2, "an empty line is not a preference"
    assert all(found.expires_at is None for found in stored)
    assert not await memory.recall(MemoryQuery(kinds=frozenset({MemoryKind.SEMANTIC}))), (
        "a preference is about the person, not about the workspace it was said in"
    )


async def test_a_preference_is_read_in_a_workspace_it_was_not_stated_in() -> None:
    """§15.4: USER scope is above the workspace boundary, on purpose."""
    memory = InMemoryMemory()

    await MemoryRecorder(memory).record_preferences(
        ("always answer in Markdown",), workspace_id=WorkspaceId("work")
    )

    elsewhere = await memory.recall(
        MemoryQuery(
            workspace_id=WorkspaceId("personal"),
            scopes=frozenset({MemoryScope.WORKSPACE, MemoryScope.USER}),
        )
    )
    assert [item.content for item in elsewhere] == ["The user prefers: always answer in Markdown"]
    assert elsewhere[0].metadata["stated_in"] == "work", "and it says where it was said"


async def test_a_step_note_is_private_short_lived_and_tied_to_its_task() -> None:
    memory = InMemoryMemory()
    employee = definition()
    task = Task.create("Look something up")

    await MemoryRecorder(memory).note_step(
        task, employee, Observation(step=1, summary="web.search returned 3 results")
    )

    [note] = await memory.recall(
        MemoryQuery(
            scopes=frozenset({MemoryScope.EMPLOYEE_PRIVATE}),
            employee_id=employee.id,
            task_id=task.id,
        )
    )
    assert note.kind is MemoryKind.WORKING
    assert note.expires_at is not None
    assert await memory.recall(
        MemoryQuery(
            scopes=frozenset({MemoryScope.EMPLOYEE_PRIVATE}),
            employee_id=employee.id,
            task_id=uuid4(),
        )
    ) == [], "another task does not read this task's working notes"


# --- Consolidation (§9.7) -----------------------------------------------------


async def test_enough_episodes_are_folded_into_one_and_the_originals_go() -> None:
    memory = InMemoryMemory()
    for index in range(14):
        await memory.remember(
            item(f"Task: sort folder {index}\nOutcome (COMPLETED): sorted",
                 created_at=NOW - timedelta(days=30 - index))
        )
    llm = FakeLLM([reply("Sorting folders here always works and takes one step.")])

    folded = await Consolidator(llm, memory, memory, threshold=12, batch=8).consolidate()

    assert folded is not None and folded.kind is MemoryKind.SEMANTIC
    left = await memory.recall(MemoryQuery(limit=50))
    assert len(left) == 14 - 8 + 1
    assert any("always works" in found.content for found in left)


async def test_too_few_episodes_are_left_alone() -> None:
    memory = InMemoryMemory()
    for index in range(3):
        await memory.remember(item(f"Something happened {index}"))
    llm = FakeLLM()  # a call would raise: it must not be made

    assert await Consolidator(llm, memory, memory, threshold=12).consolidate() is None
    assert llm.call_count == 0


async def test_a_summariser_that_fails_leaves_memory_untouched() -> None:
    memory = InMemoryMemory()
    for index in range(14):
        await memory.remember(item(f"Episode {index}"))
    from tests.fakes.llm import transient

    folded = await Consolidator(
        FakeLLM([transient()]), memory, memory, threshold=12
    ).consolidate()

    assert folded is None
    assert len(await memory.recall(MemoryQuery(limit=50))) == 14


async def test_a_broken_memory_never_fails_the_work() -> None:
    class Broken:
        async def remember(self, item):
            raise RuntimeError("the disk is gone")

        async def recall(self, query):
            raise RuntimeError("the disk is gone")

    task = Task.create("Something")
    # No exception escapes: a run with a broken memory is a run that goes
    # slightly worse, not a run that stops.
    await MemoryRecorder(Broken()).note_step(
        task, definition(), Observation(step=1, summary="s")
    )


# --- What is stored is not what was said (the second run's finding) -----------


async def test_the_employees_closing_message_is_not_what_gets_remembered() -> None:
    """A model's last message is written for the person who asked, now.

    Stored as it stands it reads as narration - "Perfect, I have everything I
    need... ## Task Complete" - and a later run given that plans a report about
    the report. Both validation runs did exactly that; this is the fix, as a
    test.
    """
    from application.memory.distiller import OutcomeDistiller

    memory = InMemoryMemory()
    llm = FakeLLM([reply("The sales folder holds two Q3 CSVs and a README naming the columns.")])
    task = _finished_task("Summarise the sales folder", "Perfect! ## Task Complete Done.")

    await MemoryRecorder(memory, distiller=OutcomeDistiller(llm)).record_task(
        task, definition("analyst")
    )

    [remembered] = await memory.recall(MemoryQuery(text="sales folder", limit=1))
    assert remembered.content.startswith("The sales folder holds two Q3 CSVs")
    assert "Task Complete" not in remembered.content
    assert "Summarise the sales folder" in remembered.content, "still findable by its goal"


async def test_a_distiller_that_cannot_be_reached_stores_the_raw_result() -> None:
    """A memory that reads badly beats no memory at all."""
    from application.memory.distiller import OutcomeDistiller
    from tests.fakes.llm import transient

    memory = InMemoryMemory()
    task = _finished_task("Count the invoices", "There are twelve invoices.")

    await MemoryRecorder(memory, distiller=OutcomeDistiller(FakeLLM([transient()]))).record_task(
        task, definition("analyst")
    )

    [remembered] = await memory.recall(MemoryQuery(text="invoices", limit=1))
    assert "twelve invoices" in remembered.content


def _finished_task(goal: str, summary: str) -> Task:
    task = Task.create(goal)
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.VERIFYING):
        task, _ = task.transition_to(status)
    task, _ = task.transition_to(TaskStatus.COMPLETED, result=TaskResult(summary=summary))
    return task


# --- 12.4: a plan's memory belongs to that plan -------------------------------


async def test_a_plan_never_reads_another_plans_memory() -> None:
    """The isolation test Phase 12's Definition of Done asks for."""
    memory = InMemoryMemory()
    ours, theirs = uuid4(), uuid4()
    for plan_id, text in ((ours, "our finding"), (theirs, "their finding")):
        await memory.remember(
            MemoryItem.create(
                text,
                scope=MemoryScope.PLAN,
                kind=MemoryKind.EPISODIC,
                plan_id=plan_id,
            )
        )

    found = await memory.recall(
        MemoryQuery(
            text="finding", scopes=frozenset({MemoryScope.PLAN}), plan_id=ours
        )
    )

    assert [item.content for item in found] == ["our finding"]


async def test_a_query_that_names_no_plan_reads_no_plan_memory() -> None:
    """A boundary with a hole for the callers who do not mention it is not one.

    A standalone `run-task` has no plan, and under the weaker reading it was the
    one caller that could read every plan's memory at once.
    """
    memory = InMemoryMemory()
    await memory.remember(
        MemoryItem.create(
            "somebody else's plan",
            scope=MemoryScope.PLAN,
            kind=MemoryKind.EPISODIC,
            plan_id=uuid4(),
        )
    )

    found = await memory.recall(
        MemoryQuery(text="plan", scopes=frozenset({MemoryScope.PLAN}), plan_id=None)
    )

    assert found == []
