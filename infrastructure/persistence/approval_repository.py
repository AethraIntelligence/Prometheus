"""Approval storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.approvals.models import (
    Approval,
    ApprovalGrant,
    ApprovalRequest,
    ApprovalScope,
    ApprovalState,
    CapabilityLease,
)
from domain.errors import StorageError, StorageNotInitializedError
from domain.policies.models import RiskLevel
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ApprovalRow, CapabilityLeaseRow
from infrastructure.persistence.session import session_scope


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _to_values(approval: Approval) -> dict:
    request = approval.request
    return {
        "id": str(request.id),
        "workspace_id": str(request.workspace_id),
        "task_id": str(request.task_id),
        "requested_by_employee_id": (
            str(request.requested_by_employee_id) if request.requested_by_employee_id else None
        ),
        "action": request.action,
        "tool": request.tool,
        "payload": request.payload,
        "risk_level": request.risk_level.value,
        "state": approval.state.value,
        "reason": request.reason,
        "subject": request.scope.subject if request.scope else "",
        "resource": request.scope.resource if request.scope else "",
        "limits": request.scope.limits if request.scope else {},
        "preview": request.preview,
        "policy_source": request.policy_source,
        "requires_explicit_confirmation": request.requires_explicit_confirmation,
        "context_sources": list(request.context_sources),
        "requested_at": request.requested_at,
        "expires_at": request.expires_at,
        "resolved_at": approval.resolved_at,
        "resolved_by": approval.resolved_by,
        "comment": approval.comment,
        "grant_kind": approval.grant.value,
        "lease_id": str(approval.lease_id) if approval.lease_id else None,
    }


def _to_approval(row: ApprovalRow) -> Approval:
    return Approval(
        request=ApprovalRequest(
            id=UUID(row.id),
            task_id=UUID(row.task_id),
            action=row.action,
            tool=row.tool or "",
            payload=row.payload or {},
            risk_level=RiskLevel(row.risk_level),
            workspace_id=WorkspaceId(row.workspace_id),
            requested_by_employee_id=(
                UUID(row.requested_by_employee_id) if row.requested_by_employee_id else None
            ),
            requested_at=_aware(row.requested_at),  # type: ignore[arg-type]
            reason=row.reason or "",
            scope=(
                ApprovalScope(
                    subject=row.subject,
                    action=row.tool or "",
                    resource=row.resource,
                    limits=row.limits or {},
                )
                if row.subject and row.tool and row.resource
                else None
            ),
            preview=row.preview or {},
            policy_source=row.policy_source or "risk_threshold",
            requires_explicit_confirmation=bool(row.requires_explicit_confirmation),
            context_sources=tuple(row.context_sources or ()),
            expires_at=_aware(row.expires_at),
        ),
        state=ApprovalState(row.state),
        resolved_at=_aware(row.resolved_at),
        resolved_by=row.resolved_by,
        comment=row.comment or "",
        grant=ApprovalGrant(row.grant_kind or ApprovalGrant.ONCE.value),
        lease_id=UUID(row.lease_id) if row.lease_id else None,
    )


def _lease_values(lease: CapabilityLease) -> dict:
    return {
        "id": str(lease.id),
        "workspace_id": str(lease.workspace_id),
        "subject": lease.scope.subject,
        "action": lease.scope.action,
        "resource": lease.scope.resource,
        "limits": lease.scope.limits,
        "grant_kind": lease.grant.value,
        "reason": lease.reason,
        "approval_id": str(lease.approval_id),
        "task_id": str(lease.task_id) if lease.task_id else None,
        "created_at": lease.created_at,
        "expires_at": lease.expires_at,
        "revoked_at": lease.revoked_at,
        "revoked_by": lease.revoked_by,
    }


def _to_lease(row: CapabilityLeaseRow) -> CapabilityLease:
    return CapabilityLease(
        id=UUID(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        scope=ApprovalScope(
            subject=row.subject,
            action=row.action,
            resource=row.resource,
            limits=row.limits or {},
        ),
        grant=ApprovalGrant(row.grant_kind),
        reason=row.reason,
        approval_id=UUID(row.approval_id),
        task_id=UUID(row.task_id) if row.task_id else None,
        created_at=_aware(row.created_at),  # type: ignore[arg-type]
        expires_at=_aware(row.expires_at),
        revoked_at=_aware(row.revoked_at),
        revoked_by=row.revoked_by,
    )


class SqlApprovalRepository:
    """Implements `domain.approvals.protocols.ApprovalRepository`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        try:
            async with session_scope(self._session_factory) as session:
                yield session
        except OperationalError as error:
            message = str(error.orig)
            if "no such table" in message or "unable to open database file" in message:
                raise StorageNotInitializedError("The local database has no schema yet.") from error
            raise StorageError(message) from error

    async def save(self, approval: Approval) -> None:
        values = _to_values(approval)
        async with self._session() as session:
            statement = upsert(session, ApprovalRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ApprovalRow.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "requested_at")},
                )
            )

    async def get(self, approval_id: UUID) -> Approval | None:
        async with self._session() as session:
            row = await session.get(ApprovalRow, str(approval_id))
            return _to_approval(row) if row else None

    async def expire_overdue(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        async with self._session() as session:
            result = await session.execute(
                update(ApprovalRow)
                .where(
                    ApprovalRow.state == ApprovalState.PENDING.value,
                    ApprovalRow.expires_at.is_not(None),
                    ApprovalRow.expires_at <= moment,
                )
                .values(
                    state=ApprovalState.EXPIRED.value,
                    resolved_at=moment,
                    resolved_by="timeout",
                )
            )
            return int(result.rowcount or 0)

    async def expire_abandoned(self) -> int:
        moment = datetime.now(UTC)
        async with self._session() as session:
            result = await session.execute(
                update(ApprovalRow)
                .where(ApprovalRow.state == ApprovalState.PENDING.value)
                .values(
                    state=ApprovalState.EXPIRED.value,
                    resolved_at=moment,
                    resolved_by="restart",
                    comment="The process stopped before this decision was answered.",
                )
            )
            return int(result.rowcount or 0)

    async def for_task(self, task_id: UUID) -> list[Approval]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.task_id == str(task_id))
                .order_by(ApprovalRow.requested_at)
            )
            return [_to_approval(row) for row in rows]

    async def list_pending(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Approval]:
        await self.expire_overdue()
        async with self._session() as session:
            rows = await session.scalars(
                select(ApprovalRow)
                .where(
                    ApprovalRow.state == ApprovalState.PENDING.value,
                    ApprovalRow.workspace_id == str(workspace_id),
                )
                .order_by(ApprovalRow.requested_at)
            )
            return [_to_approval(row) for row in rows]

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Approval]:
        await self.expire_overdue()
        async with self._session() as session:
            rows = await session.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.workspace_id == str(workspace_id))
                .order_by(ApprovalRow.requested_at.desc())
                .limit(limit)
            )
            return [_to_approval(row) for row in rows]


class InMemoryApprovalRepository:
    """Implements `domain.approvals.protocols.ApprovalRepository`."""

    def __init__(self) -> None:
        self._approvals: dict[UUID, Approval] = {}

    async def save(self, approval: Approval) -> None:
        self._approvals[approval.id] = approval

    async def get(self, approval_id: UUID) -> Approval | None:
        return self._approvals.get(approval_id)

    async def expire_overdue(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        overdue = [a for a in self._approvals.values() if a.is_overdue(moment)]
        for approval in overdue:
            self._approvals[approval.id] = approval.expire(moment)
        return len(overdue)

    async def expire_abandoned(self) -> int:
        pending = [approval for approval in self._approvals.values() if approval.is_pending]
        for approval in pending:
            self._approvals[approval.id] = approval.resolve(
                ApprovalState.EXPIRED,
                resolved_by="restart",
                comment="The process stopped before this decision was answered.",
            )
        return len(pending)

    async def for_task(self, task_id: UUID) -> list[Approval]:
        return sorted(
            (
                approval
                for approval in self._approvals.values()
                if approval.request.task_id == task_id
            ),
            key=lambda approval: approval.request.requested_at,
        )

    async def list_pending(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Approval]:
        await self.expire_overdue()
        return sorted(
            (
                approval
                for approval in self._approvals.values()
                if approval.is_pending and approval.request.workspace_id == workspace_id
            ),
            key=lambda approval: approval.request.requested_at,
        )

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Approval]:
        await self.expire_overdue()
        found = sorted(
            (
                approval
                for approval in self._approvals.values()
                if approval.request.workspace_id == workspace_id
            ),
            key=lambda approval: approval.request.requested_at,
            reverse=True,
        )
        return found[:limit]


class SqlCapabilityLeaseRepository:
    """Exact grant storage with matching performed against indexed columns."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, lease: CapabilityLease) -> None:
        values = _lease_values(lease)
        async with session_scope(self._session_factory) as session:
            statement = upsert(session, CapabilityLeaseRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[CapabilityLeaseRow.id],
                    set_={key: value for key, value in values.items() if key != "id"},
                )
            )

    async def get(self, lease_id: UUID) -> CapabilityLease | None:
        async with session_scope(self._session_factory) as session:
            row = await session.get(CapabilityLeaseRow, str(lease_id))
            return _to_lease(row) if row else None

    async def find_match(
        self,
        scope: ApprovalScope,
        *,
        workspace_id: WorkspaceId,
        task_id: UUID,
        now: datetime | None = None,
    ) -> CapabilityLease | None:
        moment = now or datetime.now(UTC)
        async with session_scope(self._session_factory) as session:
            rows = await session.scalars(
                select(CapabilityLeaseRow)
                .where(
                    CapabilityLeaseRow.workspace_id == str(workspace_id),
                    CapabilityLeaseRow.subject == scope.subject,
                    CapabilityLeaseRow.action == scope.action,
                    CapabilityLeaseRow.resource == scope.resource,
                    CapabilityLeaseRow.revoked_at.is_(None),
                    or_(
                        CapabilityLeaseRow.expires_at.is_(None),
                        CapabilityLeaseRow.expires_at > moment,
                    ),
                    or_(
                        CapabilityLeaseRow.grant_kind == ApprovalGrant.PERSISTENT.value,
                        CapabilityLeaseRow.task_id == str(task_id),
                    ),
                )
                .order_by(CapabilityLeaseRow.created_at.desc())
            )
            for row in rows:
                lease = _to_lease(row)
                if lease.matches(
                    scope, workspace_id=workspace_id, task_id=task_id, now=moment
                ):
                    return lease
        return None

    async def list_active(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[CapabilityLease]:
        moment = datetime.now(UTC)
        async with session_scope(self._session_factory) as session:
            rows = await session.scalars(
                select(CapabilityLeaseRow)
                .where(
                    CapabilityLeaseRow.workspace_id == str(workspace_id),
                    CapabilityLeaseRow.revoked_at.is_(None),
                    or_(
                        CapabilityLeaseRow.expires_at.is_(None),
                        CapabilityLeaseRow.expires_at > moment,
                    ),
                )
                .order_by(CapabilityLeaseRow.created_at.desc())
            )
            return [_to_lease(row) for row in rows]

    async def revoke(self, lease_id: UUID, *, by: str = "user") -> bool:
        async with session_scope(self._session_factory) as session:
            result = await session.execute(
                update(CapabilityLeaseRow)
                .where(
                    CapabilityLeaseRow.id == str(lease_id),
                    CapabilityLeaseRow.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC), revoked_by=by)
            )
            return bool(result.rowcount)


class InMemoryCapabilityLeaseRepository:
    def __init__(self) -> None:
        self._leases: dict[UUID, CapabilityLease] = {}

    async def save(self, lease: CapabilityLease) -> None:
        self._leases[lease.id] = lease

    async def get(self, lease_id: UUID) -> CapabilityLease | None:
        return self._leases.get(lease_id)

    async def find_match(
        self,
        scope: ApprovalScope,
        *,
        workspace_id: WorkspaceId,
        task_id: UUID,
        now: datetime | None = None,
    ) -> CapabilityLease | None:
        matches = [
            lease
            for lease in self._leases.values()
            if lease.matches(
                scope, workspace_id=workspace_id, task_id=task_id, now=now
            )
        ]
        return max(matches, key=lambda lease: lease.created_at, default=None)

    async def list_active(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[CapabilityLease]:
        return sorted(
            (
                lease
                for lease in self._leases.values()
                if lease.workspace_id == workspace_id and lease.is_active()
            ),
            key=lambda lease: lease.created_at,
            reverse=True,
        )

    async def revoke(self, lease_id: UUID, *, by: str = "user") -> bool:
        lease = self._leases.get(lease_id)
        if lease is None or not lease.is_active():
            return False
        self._leases[lease_id] = lease.revoke(by=by)
        return True
