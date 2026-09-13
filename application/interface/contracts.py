"""What an interface hands in, in a form that does not name the interface.

One type crosses the boundary inwards: `UserRequest`. Everything the platform
needs to start work is on it, and nothing about where it came from changes what
happens next - which is the whole point. `source` is recorded, never branched
on. The first `if request.source is RequestSource.DESKTOP:` anywhere below this
file is the moment the core acquires a favourite interface, and every other one
starts being the degraded path.

`input_type` and `attachments` are declared now and mostly unused now. That is
deliberate and it is cheap: they are fields on a frozen dataclass, not
machinery. Voice is the case worth naming - a spoken request becomes a
`UserRequest` with `input_type=VOICE` and text already transcribed by whatever
adapter heard it, so speech never becomes something the core has to know about.
The alternative, adding the field when voice arrives, means changing every
adapter at the moment there is least slack to do it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from domain.workforce.directions import NONE, ApprovalChoice, Directions
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

__all__ = [
    "ApprovalChoice",
    "Attachment",
    "Directions",
    "InputType",
    "RequestSource",
    "UserRequest",
]


class RequestSource(StrEnum):
    """Where a request came in. Recorded for the trace; never branched on."""

    DESKTOP = "desktop"
    WEB = "web"
    CLI = "cli"
    API = "api"
    TELEGRAM = "telegram"
    SCHEDULE = "schedule"


class InputType(StrEnum):
    """What the user gave, before an adapter turned it into text.

    An adapter is responsible for producing `content`: a voice request arrives
    transcribed, an image request arrives with the image as an attachment and a
    sentence about it. The core reads text and the attachments it was handed.
    """

    TEXT = "text"
    VOICE = "voice"
    IMAGE = "image"
    FILE = "file"
    STRUCTURED = "structured"


@dataclass(frozen=True, slots=True)
class Attachment:
    """Something the request came with, by reference rather than by value.

    A path, not bytes. The platform is local-first: the file the user dropped
    on the window is already on this machine, and copying it through the
    request would make the interface a second file store.
    """

    path: str
    media_type: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class UserRequest:
    """One thing a person asked for, from wherever they asked it."""

    content: str
    source: RequestSource = RequestSource.API
    input_type: InputType = InputType.TEXT
    conversation_id: UUID | None = None
    attachments: tuple[Attachment, ...] = ()
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    #: How to go about it - whether to ask before an action that needs approval,
    #: and which model to prefer. Beside the text rather than inside it, so that
    #: something other than a model reads it (`domain/workforce/directions.py`).
    directions: Directions = NONE
    #: Anything the adapter wants kept with the request and does not want the
    #: core to interpret - a window id, a chat id, a client version.
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def text(self) -> str:
        """The request as the platform reads it: trimmed, never rewritten."""
        return self.content.strip()
