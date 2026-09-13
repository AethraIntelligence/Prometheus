"""Conversation storage. SQL does not leave this package.

Short, because a conversation is short: an id, a name and when it was last
spoken in. What was actually said is read from `objectives`, which is where it
already is - see migration 012 for why there is no `messages` table here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.conversations.models import Conversation
from domain.errors import StorageError, StorageNotInitializedError
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ConversationRow
from infrastructure.persistence.session import session_scope


def _to_row(conversation: Conversation) -> dict[str, object]:
    return {
        "id": str(conversation.id),
        "workspace_id": str(conversation.workspace_id),
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _to_conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        id=UUID(row.id),
        title=row.title,
        workspace_id=WorkspaceId(row.workspace_id),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SqlConversationRepository:
    """Implements `domain.conversations.repository.ConversationRepository`."""

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

    async def save(self, conversation: Conversation) -> None:
        values = _to_row(conversation)
        async with self._session() as session:
            statement = upsert(session, ConversationRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ConversationRow.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "created_at")},
                )
            )

    async def get(self, conversation_id: UUID) -> Conversation | None:
        async with self._session() as session:
            row = await session.get(ConversationRow, str(conversation_id))
            return _to_conversation(row) if row else None

    async def delete(self, conversation_id: UUID) -> bool:
        async with self._session() as session:
            row = await session.get(ConversationRow, str(conversation_id))
            if row is None:
                return False
            await session.delete(row)
            return True

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Conversation]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ConversationRow)
                .where(ConversationRow.workspace_id == str(workspace_id))
                .order_by(ConversationRow.updated_at.desc(), ConversationRow.id.desc())
                .limit(limit)
            )
            return [_to_conversation(row) for row in rows]


class InMemoryConversationRepository:
    """Implements `domain.conversations.repository.ConversationRepository`."""

    def __init__(self) -> None:
        self._conversations: dict[UUID, Conversation] = {}

    async def save(self, conversation: Conversation) -> None:
        self._conversations[conversation.id] = deepcopy(conversation)

    async def get(self, conversation_id: UUID) -> Conversation | None:
        found = self._conversations.get(conversation_id)
        return deepcopy(found) if found else None

    async def delete(self, conversation_id: UUID) -> bool:
        return self._conversations.pop(conversation_id, None) is not None

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Conversation]:
        newest = sorted(
            self._conversations.values(), key=lambda c: c.updated_at, reverse=True
        )
        return [deepcopy(c) for c in newest if c.workspace_id == workspace_id][:limit]
