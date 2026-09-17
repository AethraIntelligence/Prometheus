"""Approval storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.approvals.models import Approval, ApprovalRequest, ApprovalState
from domain.errors import StorageError, StorageNotInitializedError
from domain.policies.models import RiskLevel
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ApprovalRow
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
        "requested_at": request.requested_at,
        "expires_at": request.expires_at,
        "resolved_at": approval.resolved_at,
        "resolved_by": approval.resolved_by,
        "comment": approval.comment,
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
            expires_at=_aware(row.expires_at),
        ),
        state=ApprovalState(row.state),
        resolved_at=_aware(row.resolved_at),
        resolved_by=row.resolved_by,
        comment=row.comment or "",
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
