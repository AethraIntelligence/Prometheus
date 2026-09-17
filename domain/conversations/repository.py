"""Where conversations are kept. SQL lives behind this, never in front of it."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from domain.conversations.models import Conversation
from domain.conversations.session import SessionState
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class ConversationRepository(Protocol):
    async def save(self, conversation: Conversation) -> None: ...

    async def get(self, conversation_id: UUID) -> Conversation | None: ...

    async def delete(self, conversation_id: UUID) -> bool:
        """Remove the thread. The objectives asked in it are history and stay."""
        ...

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Conversation]:
        """Most recently spoken in first."""
        ...


class SessionStateRepository(Protocol):
    """The stored half of a thread's brief (`domain/conversations/session.py`)."""

    async def get(self, conversation_id: UUID) -> SessionState | None: ...

    async def save(self, state: SessionState) -> None: ...
