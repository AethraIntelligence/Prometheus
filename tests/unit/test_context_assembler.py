"""What a run is told, and where each part of it came from (§9.6, §15.6)."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from application.employee_runtime.executor import Executor
from application.employee_runtime.planner import Planner
from application.employee_runtime.runtime import EmployeeRuntime, RuntimeDependencies
from application.employee_runtime.verifier import Verifier
from application.memory.assembler import ContextAssembler
from application.memory.recorder import MemoryRecorder
from domain.employees.limits import ExecutionLimits
from domain.knowledge.models import Chunk, Document, Passage
from domain.memory.models import MemoryItem, MemoryKind, MemoryQuery, MemoryScope
from domain.tasks.task import Task, TaskStatus
from domain.workforce.assignment import SharedContext
from infrastructure.memory.in_memory import InMemoryMemory
from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
from infrastructure.tools.registry import InMemoryToolRegistry
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply

PLAN = reply('{"steps": [{"description": "Answer it", "expected_outcome": "an answer"}]}')
PASS = reply('{"passed": true, "reason": "good enough"}')


def claims(assembled) -> tuple[str, ...]:
    """What was recalled, without the provenance each line now carries.

    The citation is checked on its own (`test_memory_provenance.py`); these
    tests are about which memories arrive, not how they are labelled.
    """
    lines = assembled.recollections()
    assert all(line.endswith("]") and " [" in line for line in lines)
    return tuple(line.rsplit(" [", 1)[0] for line in lines)


def build(script, memory=None, employee=None, retriever=None):
    llm = FakeLLM(script)
    registry = InMemoryToolRegistry([])
    tasks = InMemoryTaskRepository()
    who = employee or definition()
    deps = RuntimeDependencies(
        planner=Planner(llm),
        executor=Executor(llm, registry, limits=ExecutionLimits()),
        verifier=Verifier(llm),
        tasks=tasks,
        tools=registry,
        limits=ExecutionLimits(),
        context=(
            ContextAssembler(memory, retriever=retriever)
            if (memory or retriever)
            else None
        ),
        recorder=MemoryRecorder(memory) if memory else None,
    )
    return EmployeeRuntime(who, deps), tasks, llm


async def test_the_three_parts_arrive_in_order_of_trust() -> None:
    memory = InMemoryMemory()
    employee = definition()
    await memory.remember(
        MemoryItem.create(
            "The invoices live in finance/2026",
            scope=MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
        )
    )
    task = Task.create("Sort the invoices")

    assembled = await ContextAssembler(memory).assemble(
        task,
        employee,
        SharedContext(facts=("Last month's are already done",), constraints=("Keep names",)),
    )

    assert assembled.goal == task.goal
    assert assembled.facts == ("Last month's are already done",)
    assert assembled.constraints == ("Keep names",)
    assert claims(assembled) == ("The invoices live in finance/2026",)


async def test_what_is_remembered_reaches_the_model_as_recollection() -> None:
    """Not as fact: memory can be stale in a way an assignment cannot."""
    memory = InMemoryMemory()
    await memory.remember(
        MemoryItem.create(
            "The quarterly report is in reports/q3.md",
            scope=MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
        )
    )
    runtime, tasks, llm = build([PLAN, reply("Found it."), PASS], memory=memory)
    task = Task.create("Find the quarterly report")
    await tasks.save(task)

    await runtime.run(task)

    execution = llm.requests[1]  # 0 is planning, 1 is the first execution turn
    prompt = "\n".join(message.content for message in execution.messages)
    assert "reports/q3.md" in prompt
    assert "may be out of date" in prompt or "confirm" in prompt


class OnePassage:
    """Implements `domain.knowledge.protocols.Retriever` with a fixed answer."""

    def __init__(self, content: str, *, title: str = "Delivery policy") -> None:
        self._content = content
        self._title = title
        self.asked: list[str] = []

    async def retrieve(self, query):
        self.asked.append(query.text)
        document = Document.create("d", workspace_id=query.workspace_id)
        return [
            Passage(
                chunk=Chunk.create(document, 0, self._content),
                title=self._title,
                source="policies/delivery.md",
                score=1.0,
                semantic=1.0,
            )
        ]


async def test_a_document_reaches_the_model_as_a_quotation_with_its_source() -> None:
    """The fourth source, and the one that is cited rather than recalled (ADR 0016)."""
    retriever = OnePassage("Express delivery arrives the next working day.")
    runtime, tasks, llm = build([PLAN, reply("Next day."), PASS], retriever=retriever)
    task = Task.create("How fast is express delivery?")
    await tasks.save(task)

    await runtime.run(task)

    prompt = "\n".join(message.content for message in llm.requests[1].messages)
    assert "Express delivery arrives the next working day." in prompt
    assert "Delivery policy" in prompt, "a quotation carries where it came from"
    assert "EXTERNAL_CONTENT" in prompt, "and is framed as somebody else's words (§25)"
    assert retriever.asked == [task.goal]


async def test_a_document_is_not_offered_as_something_the_employee_remembers() -> None:
    memory = InMemoryMemory()
    await memory.remember(
        MemoryItem.create(
            "You wrote the delivery page last week",
            scope=MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
        )
    )
    assembled = await ContextAssembler(
        memory, retriever=OnePassage("Returns run for thirty days.")
    ).assemble(Task.create("What is the returns window?"), definition())

    assert claims(assembled) == ("You wrote the delivery page last week",)
    assert "Returns run for thirty days." in "".join(assembled.quotations())
    assert not any("Returns run" in line for line in assembled.recollections())


async def test_a_task_searches_on_its_goal_and_not_on_what_it_was_handed() -> None:
    """The question is the goal. What came with it is already an answer.

    `SharedContext.facts` carries whole passages the manager retrieved and
    framed, so searching on them searches on this search's own output: the
    question's embedding is averaged with a page of text nobody asked about,
    and the coverage floor - a share of the query's words - stops being
    reachable, which throws away every passage found on its words alone.
    """
    retriever = OnePassage("Express delivery arrives the next working day.")
    passage = (
        "<<<EXTERNAL_CONTENT origin=Some report>>>\n"
        "Quarterly revenue rose in every region except the north, where a "
        "warehouse move delayed dispatches for three weeks.\n"
        "<<<END_EXTERNAL_CONTENT>>>"
    )
    task = Task.create("How fast is express delivery?")

    await ContextAssembler(None, retriever=retriever).assemble(
        task,
        definition(),
        SharedContext(facts=(passage, "The user prefers Markdown")),
    )

    assert retriever.asked == [task.goal]


async def test_a_retrieval_that_fails_costs_the_run_its_citations_and_nothing_else() -> None:
    class Broken:
        async def retrieve(self, query):
            raise RuntimeError("the store is not answering")

    assembled = await ContextAssembler(None, retriever=Broken()).assemble(
        Task.create("Anything"), definition(), SharedContext(facts=("Told this",))
    )

    assert assembled.quotations() == ()
    assert assembled.facts == ("Told this",)


async def test_a_runtime_without_memory_is_the_runtime_it_was_before() -> None:
    runtime, tasks, llm = build([PLAN, reply("An answer."), PASS])
    task = Task.create("Explain something")
    await tasks.save(task)

    await runtime.run(task)

    prompt = "\n".join(m.content for m in llm.requests[1].messages)
    assert "what you remember from earlier work" not in prompt.lower()
    assert (await tasks.get(task.id)).status is TaskStatus.COMPLETED


async def test_a_run_writes_down_what_became_of_it() -> None:
    memory = InMemoryMemory()
    runtime, tasks, _ = build([PLAN, reply("Twelve invoices, all filed."), PASS], memory=memory)
    task = Task.create("File the invoices")
    await tasks.save(task)

    await runtime.run(task)

    remembered = await memory.recall(MemoryQuery(text="invoices"))
    assert any("Twelve invoices" in found.content for found in remembered)


async def test_an_employee_recalls_its_own_notes_and_not_a_colleagues() -> None:
    memory = InMemoryMemory()
    mine = definition("mine")
    theirs = definition("theirs")
    for who, note in ((mine, "I always start with fs.list"), (theirs, "I never use fs.list")):
        await memory.remember(
            MemoryItem.create(
                note,
                scope=MemoryScope.EMPLOYEE_PRIVATE,
                kind=MemoryKind.SEMANTIC,
                employee_id=who.id,
            )
        )

    assembled = await ContextAssembler(memory).assemble(Task.create("Use fs.list"), mine)

    assert claims(assembled) == ("I always start with fs.list",)


async def test_a_recall_that_fails_leaves_the_run_with_what_it_was_told() -> None:
    class Broken:
        async def remember(self, item): ...

        async def recall(self, query):
            raise RuntimeError("the index is corrupt")

    assembled = await ContextAssembler(Broken()).assemble(
        Task.create("Do the work"),
        definition(),
        SharedContext(facts=("This still arrives",)),
    )

    assert assembled.recalled == ()
    assert assembled.facts == ("This still arrives",)


async def test_a_narrowed_assignment_still_carries_its_facts_into_memory_recall() -> None:
    """The assignment is part of the query, not just part of the prompt."""
    memory = InMemoryMemory()
    employee = definition()
    await memory.remember(
        MemoryItem.create(
            "Ledger exports are tab separated",
            scope=MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
        )
    )
    task = Task.create("Clean up the export")

    assembled = await ContextAssembler(memory).assemble(
        task,
        employee,
        SharedContext(facts=("It is a ledger export",)),
    )

    assert claims(assembled) == ("Ledger exports are tab separated",)


async def test_a_plans_memory_follows_the_plan_and_not_the_employee() -> None:
    memory = InMemoryMemory()
    plan_id = uuid4()
    await memory.remember(
        MemoryItem.create(
            "Step one produced sources.csv",
            scope=MemoryScope.PLAN,
            kind=MemoryKind.EPISODIC,
            plan_id=plan_id,
        )
    )
    task = Task.create("Summarise the sources")
    within_plan = replace(task, plan_id=plan_id)
    outside_plan = replace(task, plan_id=uuid4())

    assembler = ContextAssembler(memory)
    assert claims(await assembler.assemble(within_plan, definition())) == (
        "Step one produced sources.csv",
    )
    assert claims(await assembler.assemble(outside_plan, definition())) == ()
