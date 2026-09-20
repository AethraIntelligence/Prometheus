"""Tracing, correcting and keeping memory, and reading a thread's brief, over HTTP.

What a person does with the phase through the window: open a memory and see
where it came from and where it was used, correct it without losing what it
said before, decide how long to keep it, see which memories an answer rested
on and why, and read what a long thread has established.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from domain.conversations.models import Conversation
from domain.memory.models import MemoryItem, MemoryKind, MemoryScope
from domain.memory.usage import MemoryUse
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from tests.e2e.test_memory_settings import create_schema, settings_for
from tests.fakes.llm import FakeLLM, reply


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    resolved = settings_for(tmp_path)
    create_schema(resolved)
    return resolved


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: FakeLLM([reply("")])  # type: ignore[method-assign]
        return container

    with TestClient(create_app(settings, build=build)) as client:
        yield client


def run(resolved: Settings, seed) -> object:
    async def _run():
        container = build_container(resolved)
        try:
            return await seed(container)
        finally:
            await container.aclose()

    return asyncio.run(_run())


def test_a_correction_supersedes_the_old_wording_and_both_can_be_traced(
    client: TestClient,
) -> None:
    original = client.post("/api/memory", json={"content": "Reports go to reports/2025"}).json()
    assert (original["basis"], original["source"]["kind"]) == ("STATED", "PERSON")

    corrected = client.put(
        f"/api/memory/{original['id']}", json={"content": "Reports go to reports/2026"}
    )
    assert corrected.status_code == 200
    new = corrected.json()

    current = client.get("/api/memory").json()["items"]
    assert [item["content"] for item in current] == ["Reports go to reports/2026"]
    history = client.get("/api/memory", params={"superseded": "true"}).json()["items"]
    assert {item["status"] for item in history} == {"ACTIVE", "SUPERSEDED"}

    old_trace = client.get(f"/api/memory/{original['id']}").json()
    assert old_trace["item"]["status"] == "SUPERSEDED"
    assert old_trace["superseded_by"]["content"] == "Reports go to reports/2026"
    new_trace = client.get(f"/api/memory/{new['id']}").json()
    assert [item["id"] for item in new_trace["derived_from"]] == [original["id"]]

    # What is already superseded is history, and history is not corrected.
    assert (
        client.put(f"/api/memory/{original['id']}", json={"content": "again"}).status_code
        == 404
    )
    assert client.get(f"/api/memory/{uuid4()}").status_code == 404


def test_how_long_a_memory_is_kept_is_a_persons_choice(client: TestClient) -> None:
    note = client.post("/api/memory", json={"content": "The VPN code rotates monthly"}).json()

    kept_a_week = client.put(f"/api/memory/{note['id']}/retention", json={"days": 7}).json()
    assert kept_a_week["expires_at"] != ""
    kept = client.put(f"/api/memory/{note['id']}/retention", json={"days": None}).json()
    assert kept["expires_at"] == ""
    assert (
        client.put(f"/api/memory/{note['id']}/retention", json={"days": 0}).status_code == 422
    )


def test_an_answer_says_which_memories_it_was_given_and_why(
    settings: Settings, client: TestClient
) -> None:
    shown = MemoryItem.create(
        "Invoices live in finance/2026", scope=MemoryScope.WORKSPACE, kind=MemoryKind.SEMANTIC
    )
    private = MemoryItem.create(
        "I parse invoices with csv",
        scope=MemoryScope.EMPLOYEE_PRIVATE,
        kind=MemoryKind.SEMANTIC,
        employee_id=uuid4(),
    )
    objective = Objective.create("Where are the invoices?")

    async def seed(container):
        await container.objective_repository.save(objective)
        for item in (shown, private):
            await container.memory.remember(item)
        await container.memory_uses.record(
            [
                MemoryUse(memory_id=shown.id, objective_id=objective.id, reader="manager",
                          reason='It mentions "invoices"; it belongs to this workspace.'),
                MemoryUse(memory_id=private.id, objective_id=objective.id, reader="manager",
                          reason="It mentions \"invoices\"; it is an employee's own note."),
            ]
        )  # fmt: skip

    run(settings, seed)

    answer = client.get(f"/api/objectives/{objective.id}/memory").json()

    assert answer["recorded"] is True
    first, second = answer["uses"]
    assert first["memory"]["content"] == "Invoices live in finance/2026"
    assert "invoices" in first["reason"]
    assert second["memory"] is None, "an employee's private note is explained, never shown"
    assert client.get(f"/api/objectives/{uuid4()}/memory").status_code == 404


def test_a_thread_says_which_turns_were_given_a_memory(
    settings: Settings, client: TestClient
) -> None:
    """The offer to explain a recollection belongs only to answers that had one.

    Read for the whole thread at once, so the detail stays a second request:
    opening "Memory used" to be told there was none is the bug this answers.
    """
    conversation = Conversation.create("Invoices")
    recalled = Objective.create("Where are the invoices?", conversation_id=conversation.id)
    alone = Objective.create("Hello", conversation_id=conversation.id)
    item = MemoryItem.create(
        "Invoices live in finance/2026", scope=MemoryScope.WORKSPACE, kind=MemoryKind.SEMANTIC
    )

    async def seed(container):
        await container.conversation_repository.save(conversation)
        await container.objective_repository.save(recalled)
        await container.objective_repository.save(alone)
        await container.memory.remember(item)
        await container.memory_uses.record(
            [
                MemoryUse(
                    memory_id=item.id,
                    objective_id=recalled.id,
                    reader="manager",
                    reason='It mentions "invoices"; it belongs to this workspace.',
                )
            ]
        )

    run(settings, seed)

    thread = client.get(f"/api/conversations/{conversation.id}").json()
    marked = {message["id"]: message["memory_used"] for message in thread["messages"]}
    assert marked == {str(recalled.id): True, str(alone.id): False}


def test_a_thread_brief_lists_decisions_and_open_questions(
    settings: Settings, client: TestClient
) -> None:
    conversation = Conversation.create("Q3 report")
    decided = Objective.create(
        "Build the Q3 report as CSV",
        conversation_id=conversation.id,
        constraints={"format": "CSV"},
    )
    escalated = Objective.create("Add the totals", conversation_id=conversation.id)
    escalated = replace(
        escalated,
        status=ObjectiveStatus.ESCALATED,
        result=ObjectiveResult(
            objective_id=escalated.id,
            summary="Could not find the totals.",
            status=ObjectiveStatus.ESCALATED,
            missing=("the Q3 totals",),
        ),
    )
    decided = replace(
        decided,
        status=ObjectiveStatus.DONE,
        result=ObjectiveResult(
            objective_id=decided.id, summary="Built it.", status=ObjectiveStatus.DONE
        ),
    )

    async def seed(container):
        await container.conversation_repository.save(conversation)
        await container.objective_repository.save(decided)
        await container.objective_repository.save(escalated)

    run(settings, seed)

    brief = client.get(f"/api/conversations/{conversation.id}/session").json()
    assert brief["goal"] == "Build the Q3 report as CSV"
    assert [note["text"] for note in brief["decisions"]] == ["format: CSV"]
    assert [note["text"] for note in brief["open_questions"]] == ["the Q3 totals"]
    assert brief["total_turns"] == 2

    settled = client.post(
        f"/api/conversations/{conversation.id}/session/resolved",
        json={"question": "the Q3 totals"},
    ).json()
    assert settled["open_questions"] == []
    assert client.get(f"/api/conversations/{uuid4()}/session").status_code == 404
