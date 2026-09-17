"""Threads, and the objectives that are their messages, on a real SQLite file.

The property being checked is the one that made this schema worth having: a
conversation stores no messages, so reading a thread reads the objectives - and
what a window shows cannot drift from the record of the work. A `messages`
table would have had to be kept in step with `objectives`, and the copy an
interface read would have been the one that went stale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.conversations.models import Conversation
from domain.workforce.directions import ApprovalChoice
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from infrastructure.persistence.conversation_repository import (
    InMemoryConversationRepository,
    SqlConversationRepository,
)
from infrastructure.persistence.objective_repository import SqlObjectiveRepository


@pytest.fixture
def repository(session_factory: async_sessionmaker[AsyncSession]):
    return SqlConversationRepository(session_factory)


@pytest.fixture
def objectives(session_factory: async_sessionmaker[AsyncSession]):
    return SqlObjectiveRepository(session_factory)


async def test_a_thread_survives_the_process_that_opened_it(repository) -> None:
    thread = (
        Conversation.create("Weekly research")
        .with_approvals(ApprovalChoice.AUTO)
        .with_model("balanced")
    )
    await repository.save(thread)

    read = await repository.get(thread.id)

    assert read is not None
    assert read.title == "Weekly research"
    assert read.approvals is ApprovalChoice.AUTO
    assert read.model == "balanced"
    assert read.created_at == thread.created_at


async def test_threads_are_listed_by_when_they_were_last_spoken_in(repository) -> None:
    """A thread picked up after a week belongs at the top, not buried by age."""
    now = datetime.now(UTC)
    old_but_active = Conversation.create("Old", created_at=now - timedelta(days=7))
    await repository.save(old_but_active.touched(now))
    await repository.save(
        Conversation.create("New", created_at=now, updated_at=now - timedelta(hours=1))
    )

    listed = await repository.list_recent()

    assert [item.title for item in listed] == ["Old", "New"]


async def test_the_messages_of_a_thread_are_its_objectives_in_the_order_asked(
    repository, objectives
) -> None:
    thread = Conversation.create()
    await repository.save(thread)
    now = datetime.now(UTC)
    first = Objective.create("What do my notes say?", conversation_id=thread.id, created_at=now)
    second = Objective.create(
        "Now summarise them", conversation_id=thread.id, created_at=now + timedelta(seconds=1)
    )
    elsewhere = Objective.create("Something else entirely")
    for objective in (second, first, elsewhere):
        await objectives.save(objective)

    thread_messages = await objectives.for_conversation(thread.id)

    assert [item.text for item in thread_messages] == [
        "What do my notes say?",
        "Now summarise them",
    ], "oldest first, which is the order it was said in"


async def test_an_answer_read_through_a_thread_is_the_objective_s_own(
    repository, objectives
) -> None:
    thread = Conversation.create()
    await repository.save(thread)
    objective = Objective.create("Sort my files", conversation_id=thread.id)
    await objectives.save(objective)
    await objectives.save(
        objective.to(
            ObjectiveStatus.DONE,
            ObjectiveResult(
                objective_id=objective.id, summary="Four folders.", status=ObjectiveStatus.DONE
            ),
        )
    )

    [message] = await objectives.for_conversation(thread.id)

    assert message.result is not None
    assert message.result.summary == "Four folders."
    assert message.conversation_id == thread.id


async def test_an_objective_stated_outside_a_thread_belongs_to_none(objectives) -> None:
    """The CLI, a schedule and an event all state a goal with no conversation.

    Not a degraded case: it is how most work starts, and a thread is a property
    of the interface that happened to be used.
    """
    await objectives.save(Objective.create("Every morning, check the news"))

    [stored] = await objectives.list_recent()

    assert stored.conversation_id is None
    assert await objectives.for_conversation(uuid4()) == []


async def test_the_in_memory_store_answers_the_same_way() -> None:
    """The fake and the real store are interchangeable, or tests prove nothing."""
    repository = InMemoryConversationRepository()
    thread = Conversation.create("Local")
    await repository.save(thread)

    assert (await repository.get(thread.id)).title == "Local"
    assert [item.id for item in await repository.list_recent()] == [thread.id]
    assert await repository.get(uuid4()) is None
