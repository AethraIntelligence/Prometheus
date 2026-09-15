"""Creating, switching and removing a workspace.

It decides nothing about how work is done. It reads and writes the records, and
tells the process's `WorkspaceContext` what exists so that the filesystem tools
resolve the right root on their next call - the same arrangement as
`refresh_grants`, and for the same reason: a switch is a person-sized event that
has to be true on the very next run rather than after a cache expires.

Three rules are worth stating because the obvious version gets each one wrong.

**Removing a workspace removes the record, not the history.** No foreign key
points at `workspaces` (migration 014), so the tasks, plans, memory and audit
lines belonging to it survive - which is what the audit table has always been
for. Deleting a person's history is a separate act that says so, and the files
on disk are not touched at all: a directory the user put documents in is theirs.

**The first workspace cannot be removed.** Every row written before Phase 15
belongs to it, and it is where a machine falls back to when the active one is
gone. Removing it would orphan the history it holds and leave nowhere to land.

**Removing the active workspace lands on the default one.** The alternative -
leaving the machine pointed at a workspace that no longer exists - would be
discovered by the next request, which is the worst moment to discover it.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from application.workspaces.folders import chosen_folder
from domain.errors import (
    DuplicateWorkspaceError,
    ProtectedWorkspaceError,
    WorkspaceNotFoundError,
)
from domain.workspace.models import (
    DEFAULT_WORKSPACE_ID,
    Workspace,
    WorkspaceId,
    slug,
)
from domain.workspace.protocols import WorkspaceContext
from domain.workspace.repository import WorkspaceRepository

log = structlog.get_logger(__name__)


class WorkspaceService:
    def __init__(
        self, *, repository: WorkspaceRepository, context: WorkspaceContext
    ) -> None:
        self._repository = repository
        self._context = context

    # --- Reading --------------------------------------------------------------

    async def list(self) -> list[Workspace]:
        await self._repository.ensure_default()
        workspaces = await self._repository.list()
        self._context.declare(workspaces)
        return workspaces

    async def get(self, workspace_id: WorkspaceId) -> Workspace | None:
        return await self._repository.get(workspace_id)

    async def active(self) -> Workspace:
        """The workspace this machine is in, which always exists.

        A switch file naming a workspace that has since been removed answers
        with the default one rather than raising: the caller asking this is
        usually about to start work, and there is a correct place to start it.
        """
        workspaces = {workspace.id: workspace for workspace in await self.list()}
        chosen = workspaces.get(self._context.active)
        if chosen is not None:
            return chosen
        log.warning("workspace.active_missing", workspace_id=str(self._context.active))
        return await self._repository.ensure_default()

    def root_for(self, workspace_id: WorkspaceId) -> Path:
        """Where that workspace's files are on this machine.

        A question about the machine rather than about the record - the record
        holds only an override - so it is answered by the context, which is what
        the filesystem tools ask on every call.
        """
        return self._context.root_for(workspace_id)

    # --- Writing --------------------------------------------------------------

    async def create(
        self, name: str, *, description: str = "", file_root: str | None = None
    ) -> Workspace:
        workspace = Workspace.create(name, description=description, file_root=file_root)
        existing = await self._repository.get(workspace.id)
        if existing is not None:
            raise DuplicateWorkspaceError(
                f"'{existing.name}' already uses the name {workspace.id}"
            )
        await self._repository.save(workspace)
        await self.list()  # the context learns the new root before anything asks
        log.info("workspace.created", workspace_id=str(workspace.id))
        return workspace

    async def update(
        self,
        workspace_id: WorkspaceId,
        *,
        name: str | None = None,
        description: str | None = None,
        file_root: str | None = None,
        folders: list[str] | None = None,
    ) -> Workspace:
        """Rename or repoint a workspace, keeping the id it is stored under.

        `folders` are the folders a person saved to work in, offered when a
        thread is pointed somewhere other than its own. A list of places to
        choose from, not a grant: a run still sees only the one folder its
        thread works in. Each is checked the way a folder chosen for a thread is.

        The id is a slug of the *original* name and is written on every row the
        workspace owns; renaming does not rewrite them. So a workspace called
        'Work' whose id is 'work' can be renamed to 'Clients' and stays 'work'
        in the database - a cosmetic mismatch, against an UPDATE across thirteen
        tables to fix something nobody asked to fix.
        """
        existing = await self._repository.get(workspace_id)
        if existing is None:
            raise WorkspaceNotFoundError(f"Unknown workspace: {workspace_id}")
        updated = Workspace(
            id=existing.id,
            name=(name.strip() if name else existing.name),
            description=existing.description if description is None else description,
            file_root=existing.file_root if file_root is None else (file_root or None),
            settings=(
                existing.settings
                if folders is None
                else {**existing.settings, "folders": _saved(folders)}
            ),
            created_at=existing.created_at,
        )
        await self._repository.save(updated)
        await self.list()
        return updated

    async def use(self, workspace_id: WorkspaceId) -> Workspace:
        """Switch this machine, having checked there is something to switch to."""
        workspace = await self._repository.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(f"Unknown workspace: {workspace_id}")
        await self.list()
        self._context.use(workspace.id)
        return workspace

    async def use_by_name(self, name: str) -> Workspace:
        """What a person types is a name; what a row is keyed by is a slug."""
        try:
            return await self.use(slug(name))
        except (ValueError, WorkspaceNotFoundError):
            pass
        for workspace in await self.list():
            if workspace.name.casefold() == name.strip().casefold():
                return await self.use(workspace.id)
        raise WorkspaceNotFoundError(f"Unknown workspace: {name}")

    async def delete(self, workspace_id: WorkspaceId) -> bool:
        if workspace_id == DEFAULT_WORKSPACE_ID:
            raise ProtectedWorkspaceError(
                "The first workspace holds everything written before there were "
                "others and cannot be removed."
            )
        removed = await self._repository.delete(workspace_id)
        if removed and self._context.active == workspace_id:
            self._context.use(DEFAULT_WORKSPACE_ID)
        await self.list()
        log.info("workspace.deleted", workspace_id=str(workspace_id), removed=removed)
        return removed


def _saved(folders: list[str]) -> list[str]:
    """Checked, absolute, in the order given, each once."""
    return list(dict.fromkeys(str(chosen_folder(item)) for item in folders if item.strip()))
