from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from domain.approvals.models import (
    Approval,
    ApprovalRequest,
    ApprovalScope,
    ApprovalState,
    CapabilityLease,
)
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class ApprovalService(Protocol):
    """Asks a human, and reports what they said.

    `request` blocks the action until it has an answer. A service that cannot
    reach a human answers REJECTED rather than APPROVED: the default for an
    irreversible action nobody confirmed is not to do it.
    """

    async def request(self, action: ApprovalRequest) -> ApprovalState: ...

    async def resolve(
        self,
        approval_id: UUID,
        decision: ApprovalState,
        *,
        resolved_by: str = "user",
        comment: str = "",
    ) -> None: ...


class ApprovalRepository(Protocol):
    async def save(self, approval: Approval) -> None: ...

    async def get(self, approval_id: UUID) -> Approval | None: ...

    async def list_pending(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Approval]: ...

    async def for_task(self, task_id: UUID) -> list[Approval]:
        """Every question this task raised, answered or not.

        Reading the whole history of one task rather than what is outstanding.
        How often a person had to step in is a property of a finished run, and
        `list_pending` - which is about what still needs someone - cannot answer
        it: by the time anyone asks, nothing is pending.
        """
        ...

    async def expire_overdue(self, now: datetime | None = None) -> int:
        """Close the questions nobody came back to, and say how many.

        Needed as a store operation rather than a pass over what was read: the
        process that asked is usually gone by the time the deadline passes, so
        nothing is holding the request in memory to time out. Its row is all
        that is left of the question, and this is what closes it.
        """
        ...

    async def expire_abandoned(self) -> int:
        """Close pending questions whose in-process waiters were lost.

        Called once while a new process starts, before it accepts work. At
        that point every persisted PENDING row belongs to the previous process;
        treating one as approved, or waiting on a Future that no longer
        exists, would both be unsafe.
        """
        ...


class CapabilityLeaseRepository(Protocol):
    """Durable exact grants. Matching and revocation are store operations."""

    async def save(self, lease: CapabilityLease) -> None: ...

    async def get(self, lease_id: UUID) -> CapabilityLease | None: ...

    async def find_match(
        self,
        scope: ApprovalScope,
        *,
        workspace_id: WorkspaceId,
        task_id: UUID,
        now: datetime | None = None,
    ) -> CapabilityLease | None: ...

    async def list_active(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[CapabilityLease]: ...

    async def revoke(self, lease_id: UUID, *, by: str = "user") -> bool: ...


class ApprovalWaiter(Protocol):
    """The other side of a question: who is holding the call while it is asked.

    `ApprovalService` is the asking. This is the answering, and it is a separate
    contract because it is a property of *this process* - a tool call parked on
    a future in memory - while the service's record outlives the process
    entirely. Anything above the adapters needs the second half to release a
    cancelled run and to answer a click, and needs it without importing the
    adapter that implements it.

    `decide` returning False is not a failure. It means no call here was parked
    on that question - a row left by a run that has already died - and the
    caller records the decision against the store instead.
    """

    def pending(self, task_id: UUID | None = None) -> list[ApprovalRequest]: ...

    def decide(self, approval_id: UUID, approved: bool) -> bool: ...

    def release(self, task_id: UUID) -> int:
        """Reject everything this task is waiting on, and say how many."""
        ...
