"""Approvals survive the process that asked the question."""

from __future__ import annotations

from uuid import uuid4

import pytest

from domain.approvals.models import (
    Approval,
    ApprovalGrant,
    ApprovalRequest,
    ApprovalState,
    CapabilityLease,
    scope_for,
)
from domain.policies.models import RiskLevel
from domain.tasks.task import Task
from infrastructure.persistence.approval_repository import (
    InMemoryApprovalRepository,
    SqlApprovalRepository,
    SqlCapabilityLeaseRepository,
)
from infrastructure.persistence.task_repository import SqlTaskRepository


@pytest.fixture
async def repository(request: pytest.FixtureRequest, sqlite_repository: SqlTaskRepository):
    """SQLite only for the parts that need a real task row behind the foreign key."""
    return SqlApprovalRepository(request.getfixturevalue("session_factory"))


async def stored_task(tasks: SqlTaskRepository) -> Task:
    task = Task.create("Tidy the folder")
    await tasks.save(task)
    return task


def pending(task: Task, action: str = "fs.write(path='notes.txt')") -> Approval:
    return Approval(
        request=ApprovalRequest.create(
            task.id,
            action,
            payload={"path": "notes.txt"},
            risk_level=RiskLevel.HIGH,
            reason="Overwrite notes.txt",
        )
    )


async def test_a_question_survives_the_process_that_asked_it(
    repository, sqlite_repository
) -> None:
    task = await stored_task(sqlite_repository)
    approval = pending(task)

    await repository.save(approval)

    reloaded = await repository.get(approval.id)
    assert reloaded.state is ApprovalState.PENDING
    assert reloaded.request.risk_level is RiskLevel.HIGH
    assert reloaded.request.reason == "Overwrite notes.txt"
    assert reloaded.request.payload == {"path": "notes.txt"}


async def test_step_up_provenance_survives_the_process_that_asked(
    repository, sqlite_repository
) -> None:
    task = await stored_task(sqlite_repository)
    approval = Approval(
        request=ApprovalRequest.create(
            task.id,
            "mail.send(to='client@example.com')",
            requires_explicit_confirmation=True,
            context_sources=(
                {"source": "mail.read", "kind": "integration", "trust": "untrusted"},
            ),
        )
    )

    await repository.save(approval)
    reloaded = await repository.get(approval.id)

    assert reloaded.request.requires_explicit_confirmation
    assert reloaded.request.context_sources == approval.request.context_sources


async def test_a_decision_replaces_the_pending_row(repository, sqlite_repository) -> None:
    task = await stored_task(sqlite_repository)
    approval = pending(task)
    await repository.save(approval)

    await repository.save(approval.resolve(ApprovalState.REJECTED, comment="not that file"))

    reloaded = await repository.get(approval.id)
    assert reloaded.state is ApprovalState.REJECTED
    assert reloaded.comment == "not that file"
    assert reloaded.resolved_at is not None
    assert await repository.list_pending() == []


async def test_pending_questions_are_listed_oldest_first(repository, sqlite_repository) -> None:
    task = await stored_task(sqlite_repository)
    for action in ("fs.write(a)", "fs.write(b)", "fs.write(c)"):
        await repository.save(pending(task, action))

    listed = await repository.list_pending()
    assert [a.request.action for a in listed] == ["fs.write(a)", "fs.write(b)", "fs.write(c)"]


async def test_an_unknown_approval_is_none(repository) -> None:
    assert await repository.get(uuid4()) is None


async def test_the_in_memory_repository_answers_the_same_way() -> None:
    memory = InMemoryApprovalRepository()
    approval = pending(Task.create("Tidy up"))

    await memory.save(approval)
    assert (await memory.list_pending())[0].id == approval.id

    await memory.save(approval.resolve(ApprovalState.APPROVED))
    assert await memory.list_pending() == []


async def test_restart_expires_every_pending_question(repository, sqlite_repository) -> None:
    task = await stored_task(sqlite_repository)
    first = pending(task, "fs.write(path='one.txt')")
    second = pending(task, "fs.delete(path='two.txt')")
    await repository.save(first)
    await repository.save(second)

    assert await repository.expire_abandoned() == 2

    stored = [await repository.get(first.id), await repository.get(second.id)]
    assert all(item is not None and item.state is ApprovalState.EXPIRED for item in stored)
    assert all(item is not None and item.resolved_by == "restart" for item in stored)


async def test_in_memory_restart_expires_pending_questions() -> None:
    repository = InMemoryApprovalRepository()
    approval = pending(Task.create("Tidy up"))
    await repository.save(approval)

    assert await repository.expire_abandoned() == 1
    stored = await repository.get(approval.id)
    assert stored is not None and stored.state is ApprovalState.EXPIRED
    assert stored.resolved_by == "restart"


async def test_an_exact_lease_is_persisted_matched_and_revoked(
    repository, sqlite_repository, session_factory
) -> None:
    task = await stored_task(sqlite_repository)
    approval = pending(task)
    await repository.save(approval)
    leases = SqlCapabilityLeaseRepository(session_factory)
    scope = scope_for(
        "employee-1",
        "fs.write",
        {"path": "reports/final.md", "content": "approved"},
    )
    lease = CapabilityLease.create(
        workspace_id=task.workspace_id,
        scope=scope,
        grant=ApprovalGrant.TASK,
        reason="The user approved this exact file for the task.",
        approval_id=approval.id,
        task_id=task.id,
    )

    await leases.save(lease)

    matched = await leases.find_match(
        scope, workspace_id=task.workspace_id, task_id=task.id
    )
    assert matched == lease
    assert await leases.list_active() == [lease]
    assert await leases.revoke(lease.id)
    assert await leases.list_active() == []
