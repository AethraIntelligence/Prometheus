"""Which workspace this process is in, and where that workspace's files are.

Three decisions are worth the words.

**The active workspace is a file, not a row.** `prometheus workspace use work`
writes it and every later process reads it, exactly as the stop signal works and
for the same reason: it is a fact about this machine rather than about the data,
and a database that is moved to another machine should not bring "which
workspace that other machine was looking at" with it. An unreadable or unknown
value falls back to the default workspace, because refusing to start over a
one-line file would be a worse failure than working in the wrong one.

**A run carries its own workspace.** `enter` sets a context variable, so a task
started in `work` keeps reading `work`'s files even if the person switches while
it runs, and two tasks running at once in different workspaces resolve the same
relative path to different files. An `asyncio` task copies the context at
creation, which is what makes that true without anybody passing it down.

**A thread's folder narrows it further.** A request carries the folder of the
thread it was asked in (`domain.workforce.directions`), and `current_root`
answers with that folder when there is one. The workspace's root is still what
a request with no thread - the CLI, a script - works in.

**Roots are declared, not queried.** The filesystem tools ask for the current
root on every call, and a call cannot wait on a database read; so the
application hands the known workspaces in whenever they change, the same way it
refreshes integration grants. A workspace nobody declared gets the derived
path anyway rather than an error - the map is a cache of names, not the
authority on whether the directory may exist.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from domain.workforce import directions
from domain.workspace.models import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceId
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

ACTIVE_WORKSPACE_FILE_NAME = "ACTIVE_WORKSPACE"

_current: ContextVar[WorkspaceId | None] = ContextVar("prometheus_workspace", default=None)


class LocalWorkspaceContext:
    """Implements `domain.workspace.protocols.WorkspaceContext`."""

    def __init__(
        self,
        *,
        path: Path,
        default_root: Path,
        roots_base: Path,
        configured: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> None:
        self._path = path
        # The root this machine is configured with belongs to the first
        # workspace: every installation until now has been that workspace, and
        # its files must not move because a second one was created.
        self._default_root = default_root
        self._roots_base = roots_base
        self._configured = configured
        self._declared: dict[WorkspaceId, Workspace] = {}

    # --- Which workspace ------------------------------------------------------

    @property
    def active(self) -> WorkspaceId:
        try:
            written = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return self._configured
        except OSError as error:
            log.warning(
                "workspace.active_unreadable", path=str(self._path), error=str(error)
            )
            return self._configured
        return WorkspaceId(written) if written else self._configured

    @property
    def current(self) -> WorkspaceId:
        return _current.get() or self.active

    def use(self, workspace_id: WorkspaceId) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(workspace_id), encoding="utf-8")
        log.info("workspace.switched", workspace_id=str(workspace_id))

    @contextmanager
    def enter(self, workspace_id: WorkspaceId) -> Iterator[None]:
        token = _current.set(workspace_id)
        try:
            yield
        finally:
            _current.reset(token)

    # --- Where its files are --------------------------------------------------

    def declare(self, workspaces: Iterable[Workspace]) -> None:
        self._declared = {workspace.id: workspace for workspace in workspaces}

    def root_for(self, workspace_id: WorkspaceId) -> Path:
        declared = self._declared.get(workspace_id)
        if declared is not None and declared.file_root:
            return Path(declared.file_root).expanduser()
        if workspace_id == DEFAULT_WORKSPACE_ID:
            return self._default_root
        # Beside the database rather than inside the first workspace's root: a
        # second workspace nested under the first would be readable by the
        # first's employees, and isolation that the file tools do not enforce is
        # not isolation (§15.3).
        return self._roots_base / str(workspace_id)

    def current_root(self) -> Path:
        # A thread's folder wins where the run carries one. Read here, at the
        # moment of the call, for the reason the workspace is: the tools were
        # built once and two threads run at once.
        folder = directions.current().folder
        return Path(folder).expanduser() if folder else self.root_for(self.current)


class FixedWorkspaceContext:
    """One workspace, no file, nothing switchable. For tests and a fixed root."""

    def __init__(
        self, root: Path, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> None:
        self._root = root
        self._id = workspace_id

    @property
    def active(self) -> WorkspaceId:
        return self._id

    @property
    def current(self) -> WorkspaceId:
        return self._id

    def use(self, workspace_id: WorkspaceId) -> None:
        self._id = workspace_id

    @contextmanager
    def enter(self, workspace_id: WorkspaceId) -> Iterator[None]:
        del workspace_id
        yield

    def declare(self, workspaces: Iterable[Workspace]) -> None:
        del workspaces

    def root_for(self, workspace_id: WorkspaceId) -> Path:
        del workspace_id
        return self._root

    def current_root(self) -> Path:
        return self._root
