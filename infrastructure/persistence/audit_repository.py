"""Audit storage. SQL does not leave this package.

The one behaviour worth stating: `record` never raises at its caller. An audit
line is written on the path of every tool call, and a database that has gone
away must not be able to stop work that was already approved. The failure is
logged and the action proceeds - the alternative is a platform that stops
working when its accountant does, which nobody would choose if asked.

Note that this is the *adapter's* guarantee, and the callers guard too. Both,
deliberately: the caller cannot know what a given implementation throws, and an
implementation cannot know it is the only one being called.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.audit.integrity import (
    AUDIT_CHAIN_VERSION,
    GENESIS_HASH,
    AuditVerification,
    record_hash,
)
from domain.audit.protocols import AuditRecord
from domain.errors import StorageError, StorageNotInitializedError
from domain.policies.models import ActorKind
from domain.secrets.models import redact
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.observability.logging import get_logger
from infrastructure.persistence.models import AuditCheckpointRow, AuditRow
from infrastructure.persistence.session import session_scope

log = get_logger(__name__)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _to_record(row: AuditRow) -> AuditRecord:
    return AuditRecord(
        action=row.action,
        actor_kind=ActorKind(row.actor_kind),
        result=row.result,
        actor_id=row.actor_id,
        workspace_id=WorkspaceId(row.workspace_id),
        task_id=UUID(row.task_id) if row.task_id else None,
        assignment_id=UUID(row.assignment_id) if row.assignment_id else None,
        tool=row.tool,
        model=row.model,
        cost_usd=row.cost_usd,
        latency_ms=row.latency_ms,
        details=row.details or {},
        timestamp=_aware(row.ts),
    )


class SqlAuditLog:
    """Implements `domain.audit.protocols.AuditLog` and `AuditTrail`."""

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

    async def record(self, record: AuditRecord) -> None:
        try:
            async with self._session() as session:
                safe = replace(
                    record,
                    action=redact(record.action),
                    actor_id=redact(record.actor_id) if record.actor_id else None,
                    details=redact(record.details),
                )
                workspace = str(safe.workspace_id)
                checkpoint = await session.scalar(
                    select(AuditCheckpointRow)
                    .where(AuditCheckpointRow.workspace_id == workspace)
                    .with_for_update()
                )
                previous = checkpoint.record_hash if checkpoint else GENESIS_HASH
                row = AuditRow(
                    ts=safe.timestamp,
                    workspace_id=workspace,
                    actor_kind=safe.actor_kind.value,
                    actor_id=safe.actor_id,
                    task_id=str(safe.task_id) if safe.task_id else None,
                    assignment_id=str(safe.assignment_id) if safe.assignment_id else None,
                    action=safe.action,
                    tool=safe.tool,
                    model=safe.model,
                    result=safe.result,
                    cost_usd=safe.cost_usd,
                    latency_ms=safe.latency_ms,
                    details=dict(safe.details),
                    chain_version=AUDIT_CHAIN_VERSION,
                    chain_id=safe.timestamp.astimezone(UTC).strftime("%Y-%m"),
                    previous_hash=previous,
                    record_hash="",
                )
                session.add(row)
                await session.flush()
                row.record_hash = record_hash(safe, sequence=row.id, previous_hash=previous)
                if checkpoint is None:
                    session.add(
                        AuditCheckpointRow(
                            workspace_id=workspace,
                            sequence=row.id,
                            record_hash=row.record_hash,
                            record_count=1,
                            updated_at=safe.timestamp,
                        )
                    )
                else:
                    checkpoint.sequence = row.id
                    checkpoint.record_hash = row.record_hash
                    checkpoint.record_count += 1
                    checkpoint.updated_at = safe.timestamp
        except (SQLAlchemyError, StorageError, StorageNotInitializedError) as error:
            log.warning("audit.not_recorded", action=record.action, error=str(error))

    async def recent(
        self,
        *,
        limit: int = 50,
        task_id: UUID | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> list[AuditRecord]:
        async with self._session() as session:
            statement = (
                select(AuditRow)
                .where(AuditRow.workspace_id == str(workspace_id))
                .order_by(AuditRow.ts.desc(), AuditRow.id.desc())
                .limit(limit)
            )
            if task_id is not None:
                statement = statement.where(AuditRow.task_id == str(task_id))
            rows = await session.scalars(statement)
            return [_to_record(row) for row in rows]

    async def verify(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> AuditVerification:
        async with self._session() as session:
            rows = list(
                await session.scalars(
                    select(AuditRow)
                    .where(AuditRow.workspace_id == str(workspace_id))
                    .order_by(AuditRow.id)
                )
            )
            checkpoint = await session.get(AuditCheckpointRow, str(workspace_id))
        previous = GENESIS_HASH
        for count, row in enumerate(rows, start=1):
            record = _to_record(row)
            expected = record_hash(record, sequence=row.id, previous_hash=previous)
            if row.previous_hash != previous or row.record_hash != expected:
                return AuditVerification(False, count, row.id, "record hash mismatch")
            previous = expected
        if not rows and checkpoint is None:
            return AuditVerification(True, 0)
        if checkpoint is None:
            return AuditVerification(False, len(rows), None, "checkpoint is missing")
        if (
            checkpoint.record_count != len(rows)
            or checkpoint.sequence != rows[-1].id
            or checkpoint.record_hash != previous
        ):
            return AuditVerification(False, len(rows), checkpoint.sequence, "checkpoint mismatch")
        return AuditVerification(True, len(rows))


class InMemoryAuditLog:
    """Implements the same two protocols, for tests and for a run with no store."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self._hashes: dict[WorkspaceId, list[str]] = {}

    async def record(self, record: AuditRecord) -> None:
        safe = replace(
            record,
            action=redact(record.action),
            actor_id=redact(record.actor_id) if record.actor_id else None,
            details=redact(record.details),
        )
        hashes = self._hashes.setdefault(safe.workspace_id, [])
        previous = hashes[-1] if hashes else GENESIS_HASH
        self.records.append(safe)
        hashes.append(
            record_hash(safe, sequence=len(hashes) + 1, previous_hash=previous)
        )

    async def recent(
        self,
        *,
        limit: int = 50,
        task_id: UUID | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> list[AuditRecord]:
        matching = [
            record
            for record in self.records
            if record.workspace_id == workspace_id
            and (task_id is None or record.task_id == task_id)
        ]
        return sorted(matching, key=lambda r: r.timestamp, reverse=True)[:limit]

    async def verify(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> AuditVerification:
        previous = GENESIS_HASH
        selected = [record for record in self.records if record.workspace_id == workspace_id]
        hashes = self._hashes.get(workspace_id, [])
        for index, record in enumerate(selected, start=1):
            expected = record_hash(record, sequence=index, previous_hash=previous)
            if index > len(hashes) or hashes[index - 1] != expected:
                return AuditVerification(False, index, index, "record hash mismatch")
            previous = expected
        return AuditVerification(True, len(selected))
