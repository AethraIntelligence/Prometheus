"""A conversation is a thread of requests, and nothing more.

The temptation here is a second lifecycle: messages, roles, a state machine, a
place for an assistant to store what it said. That would be a parallel history
of the same work, and the two would disagree the first time a run was resumed
or a process was killed mid-answer.

So a conversation holds no messages. **One user message is one objective** -
already recorded, already carrying the user's verbatim words, the criteria read
out of them, the plan, the tasks and the answer. This type exists to give those
objectives an order and a name; the messages are read back by asking the
objective repository which objectives belong to the thread.

The consequence worth stating: a conversation cannot drift from what actually
happened, because it stores nothing that could drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from domain.workforce.directions import ApprovalChoice
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

#: How much of the first request becomes the thread's name when nobody titled
#: it. Long enough to tell two threads apart in a list, short enough to be a
#: label rather than a paragraph.
TITLE_LIMIT = 60


class ConversationKind(StrEnum):
    """The expectation a person chose when opening a thread.

    ASK is conversation-first: answer, retrieve and explain. TASK is
    outcome-first: the same manager may still answer a trivial question
    directly, but the surface promises work, progress and artifacts when the
    request needs them. The kind belongs to the thread, not to its workspace.
    """

    ASK = "ASK"
    TASK = "TASK"


@dataclass(frozen=True, slots=True)
class Conversation:
    """A named thread of objectives, in one workspace."""

    id: UUID
    title: str = ""
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    kind: ConversationKind = ConversationKind.TASK
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: When something was last said in this thread. What a list is ordered by:
    #: a thread picked up after a week belongs at the top, and `created_at`
    #: would bury it under threads nobody has touched since.
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The directory this thread's work reads and writes, absolute. Empty until
    #: the first request, which gives it one of its own under the workspace's
    #: root (`application/workspaces/folders.py`); a person can point it
    #: elsewhere. Kept on the thread rather than chosen per request, because the
    #: second request in a thread is usually about the first one's files.
    folder: str = ""
    #: How this thread handles actions that need approval. None marks a thread
    #: created before this setting belonged to the thread; its latest request
    #: remains the backwards-compatible source in that case.
    approvals: ApprovalChoice | None = None
    #: Preferred model for future requests. None means this thread predates
    #: session-level directions and falls back to its latest request; an empty
    #: string is the explicit "Auto" choice.
    model: str | None = None

    @classmethod
    def create(cls, title: str = "", **extra: Any) -> Conversation:
        return cls(id=uuid4(), title=title.strip(), **extra)

    def touched(self, at: datetime | None = None) -> Conversation:
        return replace(self, updated_at=at or datetime.now(UTC))

    def renamed(self, title: str) -> Conversation:
        """A name the person gave, which `titled_from` then never overwrites."""
        text = " ".join(title.split())
        return replace(self, title=text) if text else self

    def titled_from(self, request: str) -> Conversation:
        """Name an untitled thread after the first thing asked in it.

        Only when it has no title: a thread the user has named keeps that name,
        and a second request must not rewrite the first one's label.
        """
        if self.title:
            return self
        text = " ".join(request.split())
        if not text:
            return self
        short = text if len(text) <= TITLE_LIMIT else text[: TITLE_LIMIT - 1].rstrip() + "…"
        return replace(self, title=short)

    def in_folder(self, folder: str) -> Conversation:
        return replace(self, folder=folder)

    def with_approvals(self, approvals: ApprovalChoice) -> Conversation:
        return replace(self, approvals=approvals)

    def with_model(self, model: str) -> Conversation:
        return replace(self, model=model.strip())
