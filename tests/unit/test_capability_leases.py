"""Exact approval scopes grant no adjacent authority."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from application.employee_runtime.approvals import ApprovalGate
from domain.approvals.models import ApprovalGrant, CapabilityLease, scope_for
from domain.policies.risk import Effect
from domain.tasks.task import Task
from infrastructure.persistence.approval_repository import (
    InMemoryCapabilityLeaseRepository,
)
from tests.fakes.approvals import ScriptedApprovalService
from tests.fakes.employees import definition
from tests.fakes.tools import FakeTool


def lease_for(
    *,
    subject: str,
    task: Task,
    path: str = "reports/final.md",
    content: str = "approved",
    grant: ApprovalGrant = ApprovalGrant.TASK,
    duration_seconds: float | None = None,
) -> CapabilityLease:
    return CapabilityLease.create(
        workspace_id=task.workspace_id,
        scope=scope_for(subject, "fs.write", {"path": path, "content": content}),
        grant=grant,
        reason="Approved for this exact file",
        approval_id=uuid4(),
        task_id=task.id if grant is ApprovalGrant.TASK else None,
        duration_seconds=duration_seconds,
    )


async def test_a_task_grant_skips_the_prompt_for_the_same_subject_action_and_resource() -> None:
    task = Task.create("Write the report")
    employee = definition("writer", tools={"fs.write"})
    leases = InMemoryCapabilityLeaseRepository()
    await leases.save(lease_for(subject=employee.actor_id, task=task))
    approvals = ScriptedApprovalService.rejecting()

    outcome = await ApprovalGate(approvals, leases=leases).check(
        FakeTool("fs.write", effect=Effect.WRITE, reversible=False),
        {"path": "reports/final.md", "content": "approved"},
        task,
        employee,
    )

    assert outcome.allowed
    assert approvals.requests == []


async def test_a_grant_for_one_path_cannot_authorize_another() -> None:
    task = Task.create("Write the report")
    employee = definition("writer", tools={"fs.write"})
    leases = InMemoryCapabilityLeaseRepository()
    await leases.save(lease_for(subject=employee.actor_id, task=task))
    approvals = ScriptedApprovalService.rejecting()

    outcome = await ApprovalGate(approvals, leases=leases).check(
        FakeTool("fs.write", effect=Effect.WRITE, reversible=False),
        {"path": "private/other.md", "content": "approved"},
        task,
        employee,
    )

    assert not outcome.allowed
    assert len(approvals.requests) == 1


async def test_a_declared_denial_is_stronger_than_a_matching_lease() -> None:
    task = Task.create("Write the report")
    employee = definition(
        "writer", tools={"fs.write"}, policies={"read_only"}
    )
    leases = InMemoryCapabilityLeaseRepository()
    await leases.save(lease_for(subject=employee.actor_id, task=task))
    approvals = ScriptedApprovalService.approving()

    outcome = await ApprovalGate(approvals, leases=leases).check(
        FakeTool("fs.write", effect=Effect.WRITE, reversible=False),
        {"path": "reports/final.md", "content": "approved"},
        task,
        employee,
    )

    assert not outcome.allowed
    assert approvals.requests == []


async def test_a_task_grant_cannot_authorize_another_task_or_employee() -> None:
    task = Task.create("Write the report")
    other_task = Task.create("Write another report")
    employee = definition("writer", tools={"fs.write"})
    other_employee = definition("editor", tools={"fs.write"})
    leases = InMemoryCapabilityLeaseRepository()
    await leases.save(lease_for(subject=employee.actor_id, task=task))

    assert (
        await leases.find_match(
            scope_for(
                employee.actor_id,
                "fs.write",
                {"path": "reports/final.md", "content": "approved"},
            ),
            workspace_id=task.workspace_id,
            task_id=other_task.id,
        )
        is None
    )
    assert (
        await leases.find_match(
            scope_for(
                other_employee.actor_id,
                "fs.write",
                {"path": "reports/final.md", "content": "approved"},
            ),
            workspace_id=task.workspace_id,
            task_id=task.id,
        )
        is None
    )


async def test_limits_expiry_and_revocation_are_enforced() -> None:
    task = Task.create("Write the report")
    employee = definition("writer", tools={"fs.write"})
    leases = InMemoryCapabilityLeaseRepository()
    lease = lease_for(subject=employee.actor_id, task=task, content="small")
    await leases.save(lease)

    too_large = scope_for(
        employee.actor_id,
        "fs.write",
        {"path": "reports/final.md", "content": "this is much larger"},
    )
    assert (
        await leases.find_match(
            too_large, workspace_id=task.workspace_id, task_id=task.id
        )
        is None
    )

    expired = lease_for(
        subject=employee.actor_id,
        task=task,
        grant=ApprovalGrant.PERSISTENT,
        duration_seconds=1,
    )
    assert not expired.is_active(expired.created_at + timedelta(seconds=2))

    assert await leases.revoke(lease.id)
    assert not (await leases.get(lease.id)).is_active()  # type: ignore[union-attr]


def test_a_domain_scope_is_normalized_and_domain_bound() -> None:
    first = scope_for("employee", "browser.open", {"url": "https://Example.com/a"})
    second = scope_for("employee", "browser.open", {"url": "https://example.com/b"})
    other = scope_for("employee", "browser.open", {"url": "https://other.example/b"})

    assert first.resource == second.resource == "domain:example.com"
    assert first.resource != other.resource
