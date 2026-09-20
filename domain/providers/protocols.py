"""How the platform learns which connections exist."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from domain.llm.catalog import ModelEntry
from domain.llm.models import TaskKind
from domain.providers.models import Connection, InstalledModels
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class ConnectionRepository(Protocol):
    async def save(self, connection: Connection) -> None: ...

    async def get(self, connection_id: UUID) -> Connection | None: ...

    async def get_by_name(
        self, name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> Connection | None: ...

    async def list(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Connection]: ...

    async def delete(self, connection_id: UUID) -> bool: ...


class CatalogAdmin(Protocol):
    """Reading and writing the catalog a person edits.

    Narrower than the store behind it: what an application service needs is the
    entries, the defaults, and the two ways to change each. Loading a catalog
    from a file is not here, because nobody above the adapters ever does that.
    """

    async def save_entry(
        self, entry: ModelEntry, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> None: ...

    async def entries(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[ModelEntry]: ...

    async def delete_entry(
        self, name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> bool: ...

    async def entries_using(
        self, connection: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[ModelEntry]: ...

    async def set_default(
        self,
        task_kind: TaskKind,
        entry_name: str,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> None: ...

    async def defaults(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> dict[TaskKind, str]: ...

    async def clear_default(self, task_kind: TaskKind) -> bool: ...


class ModelDiscovery(Protocol):
    """What a connection already has, so a person picks from a list.

    Given the kind as well as the address, because which question to ask - and
    whether there is one - depends on what is at the other end. Asking every
    address the one runner's question is what this replaced.

    `secret_name` is the *name* of the credential the connection was given, not
    its value: a service that needs a key to list what it offers is asked with
    one resolved inside the adapter at the moment of the call, and no secret
    passes through this layer (ADR 0004).
    """

    async def __call__(
        self, kind: str, base_url: str, secret_name: str = ""
    ) -> InstalledModels: ...


class ModelInspector(Protocol):
    """How much context a model takes, asked of whatever serves it.

    A person adding a local model does not know the number and should not have
    to: a default typed in its place was 8192 for a model that takes 32768, and
    the router then refused it every employee - all of whom ask for more - with
    an error nobody adding a model would connect to what they had just done.
    None means it could not be told, and the caller keeps its default.
    """

    async def context_tokens(
        self, kind: str, base_url: str, model: str, secret_name: str = ""
    ) -> int | None: ...
