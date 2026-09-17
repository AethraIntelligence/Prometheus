"""What the manager remembers, and what it hands down (§9.4).

Prometheus's half of memory is a different grain from an employee's: what it reads out
of a request - how the user wants work done here - outlives the request, and
what the workspace already knows travels to the tasks it delegates rather than
staying in the manager's own head.
"""

from __future__ import annotations

from uuid import uuid4

from application.knowledge.workspace import WorkspaceKnowledge
from application.memory.recorder import MemoryRecorder
from application.memory.workspace import WorkspaceMemory
from domain.knowledge.models import Chunk, Document, Passage
from domain.memory.models import MemoryItem, MemoryKind, MemoryQuery, MemoryScope
from domain.workforce.protocols import ObjectiveResult, ObjectiveStatus
from infrastructure.memory.in_memory import InMemoryMemory
from tests.fakes.llm import FakeLLM, reply
from tests.unit.test_prometheus_manager import build, intent, plan, verdict


def workspace_memory(memory: InMemoryMemory) -> WorkspaceMemory:
    return WorkspaceMemory(memory, MemoryRecorder(memory))


async def test_a_preference_stated_in_passing_is_kept() -> None:
    memory = InMemoryMemory()
    manager, _, _, _ = build(
        script=[
            intent(preferences=["always answer in Markdown"]),
            plan("Write the summary"),
            verdict(True),
            "Written.",
        ],
        memory=workspace_memory(memory),
    )

    await manager.handle_objective(await manager.receive("Summarise the notes"))

    kept = await memory.recall(
        MemoryQuery(
            kinds=frozenset({MemoryKind.SEMANTIC}),
            scopes=frozenset({MemoryScope.WORKSPACE, MemoryScope.USER}),
        )
    )
    assert any("Markdown" in item.content for item in kept)
    assert all(item.expires_at is None for item in kept), "a preference has no expiry"


async def test_this_requests_own_parameters_are_not_remembered_as_preferences() -> None:
    """The finding from the first validation run, as a test.

    "The sales folder" is where this job is, not how every job should be done.
    Kept as a standing preference it comes back as a workspace fact and points
    the next request at the last one's folder - which is what happened, and cost
    an objective.
    """
    memory = InMemoryMemory()
    manager, _, _, _ = build(
        script=[
            intent(constraints={"input_location": "sales folder", "output_file": "summary.md"}),
            plan("Summarise the sales folder"),
            verdict(True),
            "Summarised.",
        ],
        memory=workspace_memory(memory),
    )

    await manager.handle_objective(await manager.receive("Summarise the sales folder"))

    kept = await memory.recall(MemoryQuery(kinds=frozenset({MemoryKind.SEMANTIC}), limit=20))
    assert not any("sales" in item.content for item in kept), (
        "a constraint on this request is not a standing preference"
    )


async def test_what_the_workspace_knows_is_handed_down_with_the_work() -> None:
    """Not kept for the manager: it reaches the employee the way facts do."""
    memory = InMemoryMemory()
    await memory.remember(
        MemoryItem.create(
            "The notes live in notes/2026",
            scope=MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
        )
    )
    manager, execution, _, _ = build(
        script=[intent(), plan("Read the notes"), verdict(True), "Read them."],
        memory=workspace_memory(memory),
    )

    await manager.handle_objective(await manager.receive("Tell me what the notes say"))

    _, assignment = execution.started[0]
    # Handed down with what it rests on, so the employee reads it as a lead.
    assert any(
        fact.startswith("The notes live in notes/2026 [") for fact in assignment.context.facts
    )


async def test_a_question_answered_directly_is_still_on_the_record() -> None:
    """No task ran, so nothing else would have kept it."""
    memory = InMemoryMemory()
    manager, execution, _, _ = build(
        script=[intent(needs_work=False, answer="It is 41."), verdict(True)],
        memory=workspace_memory(memory),
    )

    result = await manager.handle_objective(await manager.receive("What is six times seven?"))

    assert result.status is ObjectiveStatus.DONE
    assert execution.started == []
    kept = await memory.recall(MemoryQuery(text="six times seven"))
    assert kept and "It is 41." in kept[0].content


async def test_a_manager_without_memory_behaves_exactly_as_it_did() -> None:
    manager, execution, _, _ = build(
        script=[intent(), plan("Read the notes"), verdict(True), "Read them."]
    )

    result = await manager.handle_objective(await manager.receive("Read the notes"))

    assert result.status is ObjectiveStatus.DONE
    _, assignment = execution.started[0]
    assert assignment.context.facts == ()


async def test_the_manager_plans_from_what_the_workspace_knows() -> None:
    """A request that leans on the last one is resolved, not investigated.

    The planner used to be the one stage that could not see memory: recalled
    context reached the employees and not the decomposition. Given "do the same
    again", it wrote a task to go and find out what "the same" had been - an
    entire employee run spent on archaeology. This is that gap, closed.
    """
    memory = InMemoryMemory()
    await memory.remember(
        MemoryItem.create(
            "The sales folder holds two Q3 CSVs and a README; summary.md documents them.",
            scope=MemoryScope.WORKSPACE,
            kind=MemoryKind.EPISODIC,
        )
    )
    llm = FakeLLM(
        [
            reply(item)
            for item in (intent(), plan("Summarise the returns folder"), verdict(True), "Done.")
        ]
    )
    manager, _, _, _ = build(script=[], llm=llm, memory=workspace_memory(memory))

    await manager.handle_objective(await manager.receive("Do the same for the returns folder."))

    # The first call is the reading of the request, the second the plan.
    for stage, request in (("the reading", llm.requests[0]), ("the plan", llm.requests[1])):
        prompt = "\n".join(message.content for message in request.messages)
        assert "summary.md documents them" in prompt, f"{stage} was made without memory"


async def test_recent_thread_turns_reach_the_next_request_without_becoming_workspace_memory(
) -> None:
    conversation_id = uuid4()
    llm = FakeLLM(
        [
            reply(intent(needs_work=False, answer="Option one is blue; option two is green.")),
            reply(verdict(True)),
            reply(intent(needs_work=False, answer="I changed option two to forest green.")),
            reply(verdict(True)),
        ]
    )
    manager, _, _, _ = build(script=[], llm=llm)

    first = await manager.receive("Give me two colour options", conversation_id=conversation_id)
    await manager.handle_objective(first)
    second = await manager.receive("Make the second one darker", conversation_id=conversation_id)
    await manager.handle_objective(second)

    second_reading = "\n".join(message.content for message in llm.requests[2].messages)
    assert "Option one is blue; option two is green." in second_reading
    assert "Give me two colour options" in second_reading


async def test_thread_context_is_bounded_to_the_nearest_eight_answered_turns() -> None:
    conversation_id = uuid4()
    manager, _, _, _ = build(script=[])
    for number in range(10):
        objective = await manager.receive(f"Request {number}", conversation_id=conversation_id)
        await manager._objectives.save(  # type: ignore[attr-defined]
            objective.to(
                ObjectiveStatus.DONE,
                result=ObjectiveResult(
                    objective_id=objective.id,
                    summary=f"Answer {number}",
                    status=ObjectiveStatus.DONE,
                ),
            )
        )
    current = await manager.receive("Continue", conversation_id=conversation_id)

    context = await manager._thread_context(current)  # type: ignore[attr-defined]

    assert len(context) == 8
    assert "Request 2" in context[0]
    assert "Request 9" in context[-1]


# --- What the user brought (§15.6) --------------------------------------------


class OneDocument:
    """Implements `domain.knowledge.protocols.Retriever` with a fixed passage."""

    def __init__(self, content: str, *, title: str = "Delivery policy") -> None:
        self._content = content
        self._title = title
        self.asked: list[tuple[str, str]] = []

    async def retrieve(self, query):
        self.asked.append((query.text, str(query.workspace_id)))
        document = Document.create("d", workspace_id=query.workspace_id)
        return [
            Passage(
                chunk=Chunk.create(document, 0, self._content),
                title=self._title,
                source="policies/delivery.md",
                score=1.0,
            )
        ]


async def test_a_document_reaches_the_reading_of_the_request() -> None:
    """The first Phase 15 validation run's finding, as a test.

    Retrieval lived only inside a task. So a question whose answer was in an
    uploaded document was read as needing no work, answered "I do not have
    that", and the document was never reached - because nothing had started a
    task to reach it with.
    """
    retriever = OneDocument("Express delivery arrives the next working day.")
    llm = FakeLLM(
        [
            reply(intent(needs_work=False, answer="Next working day.")),
            reply(verdict(True)),
        ]
    )
    manager, _, _, _ = build(script=[], llm=llm, knowledge=WorkspaceKnowledge(retriever))

    await manager.handle_objective(await manager.receive("How fast is express delivery?"))

    prompt = "\n".join(message.content for message in llm.requests[0].messages)
    assert "Express delivery arrives the next working day." in prompt
    assert "Delivery policy" in prompt, "quoted with its source, not as a recollection"
    assert retriever.asked == [("How fast is express delivery?", "default")]


async def test_documents_are_read_from_the_workspace_the_request_was_asked_in() -> None:
    """Phase 15's Definition of Done, at the level a unit test can hold it."""
    retriever = OneDocument("Express delivery arrives the next working day.")
    manager, _, _, _ = build(
        script=[
            intent(needs_work=False, answer="I do not know."),
            verdict(True),
        ],
        knowledge=WorkspaceKnowledge(retriever),
    )

    await manager.handle_objective(
        await manager.receive("How fast is express delivery?", workspace_id="personal")
    )

    assert retriever.asked[0][1] == "personal", "and never the workspace the document is in"


async def test_a_retriever_that_fails_leaves_the_manager_working() -> None:
    class Broken:
        async def retrieve(self, query):
            raise RuntimeError("the store is not answering")

    manager, _, _, _ = build(
        script=[intent(needs_work=False, answer="Answered anyway."), verdict(True)],
        knowledge=WorkspaceKnowledge(Broken()),
    )

    result = await manager.handle_objective(await manager.receive("Anything"))

    assert result.status is ObjectiveStatus.DONE
