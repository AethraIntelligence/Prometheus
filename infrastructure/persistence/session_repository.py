"""The compacted half of a thread's brief. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.conversations.session import SessionStage, SessionState
from domain.errors import StorageError, StorageNotInitializedError
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ConversationSessionRow
from infrastructure.persistence.session import session_scope


def _stage_to_json(stage: SessionStage) -> dict[str, Any]:
    return {
        "index": stage.index,
        "summary": stage.summary,
        "objective_ids": [str(item) for item in stage.objective_ids],
        "started_at": stage.started_at.isoformat(),
        "ended_at": stage.ended_at.isoformat(),
        "compacted_at": stage.compacted_at.isoformat(),
        "summarised": stage.summarised,
    }


def _stage_from_json(raw: dict[str, Any]) -> SessionStage:
    return SessionStage(
        index=int(raw["index"]),
        summary=str(raw.get("summary", "")),
        objective_ids=tuple(UUID(item) for item in raw.get("objective_ids", ())),
        started_at=_moment(raw["started_at"]),
        ended_at=_moment(raw["ended_at"]),
        compacted_at=_moment(raw["compacted_at"]),
        summarised=bool(raw.get("summarised", True)),
    )


def _moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class SqlSessionStateRepository:
    """Implements `domain.conversations.repository.SessionStateRepository`."""

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
                raise StorageNotInitializedError(
                    "The local database has no schema yet."
                ) from error
            raise StorageError(message) from error

    async def get(self, conversation_id: UUID) -> SessionState | None:
        async with self._session() as session:
            row = await session.get(ConversationSessionRow, str(conversation_id))
            if row is None:
                return None
            return SessionState(
                conversation_id=UUID(row.conversation_id),
                workspace_id=WorkspaceId(row.workspace_id),
                goal_brief=row.goal_brief,
                stages=tuple(_stage_from_json(raw) for raw in row.stages or ()),
                resolved_questions=tuple(row.resolved_questions or ()),
                updated_at=row.updated_at
                if row.updated_at.tzinfo
                else row.updated_at.replace(tzinfo=UTC),
            )

    async def save(self, state: SessionState) -> None:
        values = {
            "conversation_id": str(state.conversation_id),
            "workspace_id": str(state.workspace_id),
            "goal_brief": state.goal_brief,
            "stages": [_stage_to_json(stage) for stage in state.stages],
            "resolved_questions": list(state.resolved_questions),
            "updated_at": state.updated_at,
        }
        async with self._session() as session:
            statement = upsert(session, ConversationSessionRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ConversationSessionRow.conversation_id],
                    set_={k: v for k, v in values.items() if k != "conversation_id"},
                )
            )


class InMemorySessionStateRepository:
    """Implements `domain.conversations.repository.SessionStateRepository`."""

    def __init__(self) -> None:
        self._states: dict[UUID, SessionState] = {}

    async def get(self, conversation_id: UUID) -> SessionState | None:
        found = self._states.get(conversation_id)
        return deepcopy(found) if found else None

    async def save(self, state: SessionState) -> None:
        self._states[state.conversation_id] = deepcopy(state)
