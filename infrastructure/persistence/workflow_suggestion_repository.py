"""Workflow suggestion storage. SQL does not leave this package.

Two readings are lenient on purpose, and both fail closed - towards not showing
a suggestion. A status this build does not know reads as DISMISSED: a person may
have said no in a newer version, and repeating the question is the worse error.
A pattern written in a newer format is skipped: its steps cannot be trusted to
mean what this build would draft from them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.workflows.suggestion import (
    PATTERN_VERSION,
    StepShape,
    Suggestion,
    SuggestionStatus,
)
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import WorkflowSuggestionRow
from infrastructure.persistence.session import session_scope

log = structlog.get_logger(__name__)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _values(suggestion: Suggestion) -> dict:
    return {
        "id": str(suggestion.id),
        "workspace_id": str(suggestion.workspace_id),
        "fingerprint": suggestion.fingerprint,
        "status": suggestion.status.value,
        "pattern_version": PATTERN_VERSION,
        "steps": [step.to_dict() for step in suggestion.steps],
        "sources": [str(item) for item in suggestion.sources],
        "dismissed_sources": [str(item) for item in suggestion.dismissed_sources],
        "first_seen": suggestion.first_seen,
        "last_seen": suggestion.last_seen,
        "snoozed_until": suggestion.snoozed_until,
        "workflow_name": suggestion.workflow_name,
        "created_at": suggestion.created_at,
        "updated_at": suggestion.updated_at,
    }


def _suggestion(row: WorkflowSuggestionRow) -> Suggestion | None:
    if (row.pattern_version or 1) > PATTERN_VERSION:
        log.info("workflow_suggestion.newer_format", id=row.id, version=row.pattern_version)
        return None
    try:
        status = SuggestionStatus(row.status)
    except ValueError:
        status = SuggestionStatus.DISMISSED
    return Suggestion(
        id=UUID(row.id),
        fingerprint=row.fingerprint,
        steps=tuple(StepShape.from_dict(item) for item in row.steps or ()),
        sources=tuple(UUID(item) for item in row.sources or ()),
        first_seen=_aware(row.first_seen),  # type: ignore[arg-type]
        last_seen=_aware(row.last_seen),  # type: ignore[arg-type]
        workspace_id=WorkspaceId(row.workspace_id),
        status=status,
        dismissed_sources=tuple(UUID(item) for item in row.dismissed_sources or ()),
        snoozed_until=_aware(row.snoozed_until),
        workflow_name=row.workflow_name or "",
        created_at=_aware(row.created_at),  # type: ignore[arg-type]
        updated_at=_aware(row.updated_at),  # type: ignore[arg-type]
    )


class SqlSuggestionRepository:
    """Implements `domain.workflows.suggestion.SuggestionRepository`."""

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

    async def save(self, suggestion: Suggestion) -> None:
        values = _values(suggestion)
        async with self._session() as session:
            statement = upsert(session, WorkflowSuggestionRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[WorkflowSuggestionRow.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "created_at")},
                )
            )

    async def get(self, suggestion_id: UUID) -> Suggestion | None:
        async with self._session() as session:
            row = await session.get(WorkflowSuggestionRow, str(suggestion_id))
            return _suggestion(row) if row else None

    async def list(self, workspace_id: WorkspaceId) -> list[Suggestion]:
        async with self._session() as session:
            rows = await session.scalars(
                select(WorkflowSuggestionRow)
                .where(WorkflowSuggestionRow.workspace_id == str(workspace_id))
                .order_by(WorkflowSuggestionRow.last_seen.desc())
            )
            return [found for row in rows if (found := _suggestion(row)) is not None]


class InMemorySuggestionRepository:
    """Implements `domain.workflows.suggestion.SuggestionRepository`."""

    def __init__(self) -> None:
        self._items: dict[UUID, Suggestion] = {}

    async def save(self, suggestion: Suggestion) -> None:
        self._items[suggestion.id] = suggestion

    async def get(self, suggestion_id: UUID) -> Suggestion | None:
        return self._items.get(suggestion_id)

    async def list(self, workspace_id: WorkspaceId) -> list[Suggestion]:
        return sorted(
            (item for item in self._items.values() if item.workspace_id == workspace_id),
            key=lambda item: item.last_seen,
            reverse=True,
        )
