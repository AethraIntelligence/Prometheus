"""Adding a provider, a key and a model, and saying where work goes.

Everything a settings page does, decided here once rather than per surface. The
window, the CLI and anything written later call the same methods, so "a key is
never returned", "a connection nothing points at can be removed and one holding
up a model cannot" and "a default has to name an entry that exists" are
properties of the platform rather than of whichever page got them right.

Four rules, and each of them rejects something that would otherwise be natural.

**A key goes in and never comes back.** `store_credential` writes to the
credential store; nothing here returns a secret, and the views say only whether
one is set. A settings page that can show a key is a settings page that puts one
in a screenshot, a bug report and a log.

**A connection is checked against the adapters before it is stored.** A kind
nothing implements would sit in the window looking configured and fail at the
first call, which is the furthest possible point from the mistake.

**Removing a connection that models point at is refused, and says which.**
Deleting it silently would leave entries that route into an error, and the
person who removed a key they had finished with is the one least able to guess
why planning stopped working.

**A model is added by picking, not by typing, wherever that is possible.** A
local runner is asked what it has; a field where a person types `gemma3:27` is a
field that accepts it and fails four hours later inside a task.
"""

from __future__ import annotations

import structlog

from domain.capabilities.models import Capability
from domain.errors import ConfigurationError, NotFoundError
from domain.llm.catalog import ModelEntry
from domain.llm.models import TaskKind
from domain.providers.guide import ProviderGuide, Setup
from domain.providers.models import Connection, InstalledModels
from domain.providers.protocols import (
    CatalogAdmin,
    ConnectionRepository,
    ModelDiscovery,
    ModelInspector,
)
from domain.secrets.protocols import CredentialStore
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)


class ProviderService:
    """The operations behind Settings, and behind `prometheus providers`."""

    def __init__(
        self,
        connections: ConnectionRepository,
        catalog: CatalogAdmin,
        *,
        credentials: CredentialStore | None = None,
        kinds: tuple[object, ...] = (),
        discover: ModelDiscovery | None = None,
        inspect: ModelInspector | None = None,
        on_change: object = None,
        guide: ProviderGuide | None = None,
    ) -> None:
        self._connections = connections
        self._catalog = catalog
        self._credentials = credentials
        # What this machine can build a client for, handed in by the
        # composition root: the list belongs beside the adapters, and this layer
        # may not import them.
        self._kinds = kinds
        self._discover = discover
        self._inspect = inspect
        # Called after anything changes, so the running process picks it up
        # without a restart. A settings page whose effect begins after a restart
        # is a settings page people stop trusting.
        self._on_change = on_change
        # Advice about what to connect, handed in for the same reason the kinds
        # are: it names products, and this layer may not.
        self._guide = guide

    # --- Connections ----------------------------------------------------------

    def kinds(self) -> tuple[object, ...]:
        return self._kinds

    async def list_connections(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Connection]:
        return await self._connections.list(workspace_id)

    async def add_connection(
        self,
        name: str,
        kind: str,
        *,
        api_key: str = "",
        base_url: str = "",
        description: str = "",
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> Connection:
        """Add a way in to a provider, with its key if it needs one."""
        clean = name.strip()
        if not clean:
            raise ConfigurationError("A connection needs a name.")
        known = self._kind_named(kind)
        if await self._connections.get_by_name(clean, workspace_id) is not None:
            raise ConfigurationError(
                f"There is already a connection called '{clean}'. Names are how a "
                "model says which account it uses, so they have to be unique."
            )

        needs_credential = bool(getattr(known, "needs_credential", True))
        secret_name = ""
        if api_key.strip():
            secret_name = _secret_name_for(clean)
            await self._store_credential(secret_name, api_key.strip())
        elif needs_credential:
            raise ConfigurationError(
                f"A connection of kind '{kind}' needs an API key."
            )

        connection = Connection.create(
            clean,
            getattr(known, "name", kind),
            base_url=base_url.strip() or str(getattr(known, "default_base_url", "")),
            secret_name=secret_name,
            needs_credential=needs_credential,
            description=description.strip(),
            workspace_id=workspace_id,
        )
        await self._connections.save(connection)
        await self._changed()
        log.info("connection.added", name=clean, kind=connection.kind)
        return connection

    async def replace_key(
        self, name: str, api_key: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> Connection:
        """Rotate a credential without touching what points at the connection."""
        connection = await self._require(name, workspace_id)
        if not api_key.strip():
            raise ConfigurationError("An empty key is not a key.")
        secret_name = connection.secret_name or _secret_name_for(connection.name)
        await self._store_credential(secret_name, api_key.strip())
        updated = connection.with_secret(secret_name)
        await self._connections.save(updated)
        await self._changed()
        return updated

    async def remove_connection(
        self, name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> None:
        """Remove it, unless models are pointing at it."""
        connection = await self._require(name, workspace_id)
        holding = await self._catalog.entries_using(connection.name, workspace_id)
        if holding:
            names = ", ".join(entry.name for entry in holding)
            raise ConfigurationError(
                f"'{connection.name}' is used by {names}. Remove or repoint "
                "them first - deleting it now would leave models that cannot run."
            )
        await self._connections.delete(connection.id)
        if connection.secret_name and self._credentials is not None:
            await self._credentials.forget(connection.secret_name)
        await self._changed()
        log.info("connection.removed", name=connection.name)

    # --- Models ---------------------------------------------------------------

    async def list_models(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[ModelEntry]:
        return await self._catalog.entries(workspace_id)

    async def available_models(
        self, connection_name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> InstalledModels:
        """What this connection already has, where that can be asked."""
        connection = await self._require(connection_name, workspace_id)
        if self._discover is None:
            return InstalledModels()
        return await self._discover(connection.kind, connection.base_url)

    async def context_of(
        self, connection_name: str, model: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> int | None:
        """How much context a model on this connection takes, where that can be asked."""
        if self._inspect is None or not connection_name:
            return None
        connection = await self._require(connection_name, workspace_id)
        return await self._inspect.context_tokens(connection.kind, connection.base_url, model)

    async def add_model(
        self, entry: ModelEntry, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> ModelEntry:
        if not entry.name.strip() or not entry.model.strip():
            raise ConfigurationError("A model entry needs a name and a model.")
        if entry.connection:
            await self._require(entry.connection, workspace_id)
        await self._catalog.save_entry(entry, workspace_id)
        await self._changed()
        log.info("model.added", name=entry.name, connection=entry.connection)
        return entry

    async def remove_model(
        self, name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> None:
        if not await self._catalog.delete_entry(name, workspace_id):
            raise NotFoundError(f"No model entry called '{name}'.")
        # Work that was going here has to be un-routed, not merely ignored.
        # Loading the catalog already drops a default naming an entry that is
        # gone, so the platform behaves correctly either way - but the row
        # stayed, and every page that lists defaults showed a routing that was
        # not in force. A setting that is displayed and not obeyed is worse
        # than one that was never made.
        for kind, entry_name in (await self._catalog.defaults(workspace_id)).items():
            if entry_name == name:
                await self._catalog.clear_default(kind)
        await self._changed()

    # --- Where work goes ------------------------------------------------------

    async def defaults(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> dict[TaskKind, str]:
        return await self._catalog.defaults(workspace_id)

    async def send_work_to(
        self, task_kind: TaskKind, entry_name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> None:
        """"Give this kind of work to that model." The whole of the routing request."""
        entries = {entry.name for entry in await self._catalog.entries(workspace_id)}
        if entry_name not in entries:
            raise NotFoundError(
                f"No model entry called '{entry_name}'. Add it before sending work to it."
            )
        await self._catalog.set_default(task_kind, entry_name, workspace_id)
        await self._changed()
        log.info("work.routed", task_kind=task_kind.value, entry=entry_name)

    async def clear_default(self, task_kind: TaskKind) -> bool:
        cleared = await self._catalog.clear_default(task_kind)
        await self._changed()
        return cleared

    # --- Recommended setups --------------------------------------------------

    @property
    def guide(self) -> ProviderGuide | None:
        return self._guide

    def setup_named(self, setup_id: str) -> Setup:
        setup = self._guide.setup(setup_id) if self._guide is not None else None
        if setup is None:
            raise NotFoundError(f"No recommended setup called '{setup_id}'.")
        return setup

    async def apply_setup(
        self,
        setup_id: str,
        connection_name: str = "",
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> list[ModelEntry]:
        """Add a setup's models through one connection and send work to them.

        What a person would otherwise do by hand in two dialogs and seven
        dropdowns, decided once here. Three rules keep it safe to press twice:

        - a model already in the catalog for this provider is reused, not
          duplicated - only its routing is applied;
        - an entry name taken by a *different* model is never overwritten; the
          new entry gets a free name beside it, because the taken one may be
          what somebody's work is routed to;
        - the connection has to be of the setup's kind, and when none is named
          the first such connection is used. With none, the refusal says what
          to add.
        """
        setup = self.setup_named(setup_id)
        connection = await self._connection_for(setup, connection_name, workspace_id)
        entries = {entry.name: entry for entry in await self._catalog.entries(workspace_id)}

        added: list[ModelEntry] = []
        names: dict[str, str] = {}
        for recommended in setup.models:
            existing = next(
                (
                    entry
                    for entry in entries.values()
                    if entry.provider == setup.kind
                    and entry.model == recommended.model
                    and entry.connection in ("", connection.name)
                ),
                None,
            )
            name = existing.name if existing is not None else _free_name(recommended.name, entries)
            entry = ModelEntry(
                name=name,
                provider=setup.kind,
                model=recommended.model,
                connection=connection.name,
                capabilities=frozenset(Capability(item) for item in recommended.capabilities),
                context_tokens=recommended.context_tokens,
                input_cost_per_1k_usd=recommended.input_cost_per_1k_usd,
                output_cost_per_1k_usd=recommended.output_cost_per_1k_usd,
                quality=recommended.quality,
                dimensions=recommended.dimensions,
            )
            await self._catalog.save_entry(entry, workspace_id)
            entries[name] = entry
            names[recommended.name] = name
            added.append(entry)

        for recommended in setup.models:
            for kind in recommended.route:
                await self._catalog.set_default(
                    TaskKind(kind), names[recommended.name], workspace_id
                )
        await self._changed()
        log.info(
            "setup.applied",
            setup=setup.id,
            connection=connection.name,
            entries=[entry.name for entry in added],
        )
        return added

    def setup_applied(self, setup: Setup, entries: list[ModelEntry]) -> bool:
        """Whether every model of the setup is in the catalog for its provider."""
        present = {(entry.provider, entry.model) for entry in entries if entry.connection}
        return all((setup.kind, model.model) in present for model in setup.models)

    async def _connection_for(
        self, setup: Setup, name: str, workspace_id: WorkspaceId
    ) -> Connection:
        if name:
            connection = await self._require(name, workspace_id)
            if connection.kind != setup.kind:
                raise ConfigurationError(
                    f"'{connection.name}' is a {connection.kind} connection; "
                    f"this setup needs a {setup.kind} one."
                )
            return connection
        for connection in await self._connections.list(workspace_id):
            if connection.kind == setup.kind:
                return connection
        raise ConfigurationError(
            f"Add a {setup.kind} connection first - the models in this setup are "
            "reached through it."
        )

    # --- Inside ---------------------------------------------------------------

    def _kind_named(self, kind: str):
        wanted = kind.strip().lower()
        for known in self._kinds:
            if str(getattr(known, "name", "")).lower() == wanted:
                return known
        available = ", ".join(sorted(str(getattr(k, "name", "")) for k in self._kinds))
        raise ConfigurationError(
            f"Unknown provider kind '{kind}'. This machine has: {available or 'none'}."
        )

    async def _require(self, name: str, workspace_id: WorkspaceId) -> Connection:
        connection = await self._connections.get_by_name(name, workspace_id)
        if connection is None:
            raise NotFoundError(f"No connection called '{name}'.")
        return connection

    async def _store_credential(self, secret_name: str, value: str) -> None:
        if self._credentials is None:
            raise ConfigurationError(
                "This process cannot store credentials, so a key cannot be saved here."
            )
        await self._credentials.store(secret_name, value)

    async def _changed(self) -> None:
        if self._on_change is None:
            return
        result = self._on_change()  # type: ignore[operator]
        if hasattr(result, "__await__"):
            await result


def _free_name(wanted: str, taken: dict[str, ModelEntry]) -> str:
    if wanted not in taken:
        return wanted
    number = 2
    while f"{wanted}-{number}" in taken:
        number += 1
    return f"{wanted}-{number}"


def _secret_name_for(connection: str) -> str:
    """One credential per connection, named after it.

    Predictable rather than random: a person debugging exports
    `PROMETHEUS_SECRET_<NAME>` and it wins, which is the escape hatch the whole
    resolver ordering exists to provide.
    """
    return f"{connection.strip().lower().replace(' ', '_').replace('-', '_')}_api_key"
