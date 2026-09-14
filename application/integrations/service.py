"""Connecting a service, and everything that follows from it.

This is the lifecycle, and it decides nothing about how work is done. It adds a
record, starts a server, asks what it offers, puts the result in the registry
every other tool lives in, and takes it away again. What an employee may call,
whether an action needs approval, who does the work: all of that was decided
before this module existed and is not re-decided here.

Three things it must get right, each of which is a way the obvious version goes
wrong.

**A failure is a status, not an exception that escapes.** The person connecting
a server mistypes a command, and the answer they need is "that did not start",
attached to the record, visible next time they open the window. A traceback
reaching the interface would be a stack trace where a sentence belongs.

**Registering and forgetting are the same operation in reverse.** Every name an
integration contributed is removed when it is disabled or removed - so a server
that is gone leaves no tool behind that a model can see and nobody can call.
Names come off the *stored* record rather than off a live connection, because
the connection is exactly what is missing when it matters.

**The registry is not the source of truth.** The database is. The registry is
this process's view of it, and `restore` is how a process that has just started
gets that view - which is also why discovery is not repeated on every boot: what
a server offered is cached on the record, and starting ten subprocesses to
re-learn what is already written down would make every start-up wait for the
slowest one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from uuid import UUID

import structlog

from domain.capabilities.models import Capability
from domain.errors import (
    DuplicateIntegrationError,
    IntegrationAuthenticationError,
    IntegrationError,
    IntegrationNotFoundError,
)
from domain.integrations.models import (
    DiscoveredTool,
    Integration,
    IntegrationKind,
    IntegrationStatus,
)
from domain.integrations.protocols import Connection, Connector
from domain.integrations.repository import IntegrationRepository
from domain.policies.risk import Effect
from domain.secrets.protocols import SecretResolver
from domain.tools.protocols import ToolRegistry

log = structlog.get_logger(__name__)


class IntegrationService:
    """The operations an interface performs on connected services."""

    def __init__(
        self,
        repository: IntegrationRepository,
        registry: ToolRegistry,
        connector: Connector,
        *,
        restorer: Connector | None = None,
        secrets: SecretResolver | None = None,
        provided: Mapping[str, str] | None = None,
        on_change: Callable[[list[Integration]], None] | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._connector = connector
        # Two ways to build a connection, because start-up and connecting are
        # different questions. Connecting runs the server and asks it what it
        # offers; restoring trusts what it said last time, so a runtime with ten
        # integrations starts in the time it takes to read ten rows rather than
        # to launch ten programs. Defaults to the same one, which is what a test
        # wants and what an integration kind with no cheap path would use.
        self._restorer = restorer or connector
        self._secrets = secrets
        # What the installation supplies when nobody stored their own: the
        # platform's Google client, so signing in is a button rather than a
        # Cloud Console. Consulted after the resolver, so a person's own wins.
        self._provided = dict(provided or {})
        # Called whenever the set of integrations changes, so the employee
        # registry's snapshot of grants can follow it without polling.
        self._on_change = on_change
        self._live: dict[UUID, Connection] = {}

    # --- Reading --------------------------------------------------------------

    async def list(self) -> list[Integration]:
        return await self._repository.list()

    async def get(self, integration_id: UUID) -> Integration:
        integration = await self._repository.get(integration_id)
        if integration is None:
            raise IntegrationNotFoundError(f"no integration {integration_id}")
        return integration

    # --- Adding ---------------------------------------------------------------

    async def add(
        self,
        name: str,
        configuration: dict[str, object],
        *,
        kind: IntegrationKind = IntegrationKind.MCP,
        capabilities: frozenset[Capability] = frozenset(),
        secret_names: tuple[str, ...] = (),
        effects: dict[str, Effect] | None = None,
        granted_to: frozenset[str] = frozenset(),
    ) -> Integration:
        """Record a service. Connecting to it is a separate step on purpose.

        Adding writes down what the user typed; connecting runs a program they
        named. Keeping them apart means a server that will not start still
        leaves a record they can correct, rather than a form that clears itself.
        """
        cleaned = name.strip()
        if await self._repository.by_name(cleaned):
            raise DuplicateIntegrationError(
                f"an integration called '{cleaned}' is already connected; its tools "
                "would be indistinguishable from the new one's"
            )
        integration = Integration.create(
            cleaned,
            kind=kind,
            status=IntegrationStatus.CONFIGURING,
            configuration=dict(configuration),
            granted_capabilities=capabilities,
            secret_names=secret_names,
            effects=dict(effects or {}),
            granted_to=granted_to,
        )
        await self._repository.save(integration)
        log.info("integration.added", integration=integration.name, kind=kind.value)
        await self._changed()
        return integration

    # --- Connecting -----------------------------------------------------------

    async def connect(self, integration_id: UUID) -> Integration:
        """Start it, ask what it offers, and make that available.

        Every failure becomes a status on the record. The user is told which one
        it was because the four cases have different answers: a wrong command is
        theirs to fix, a rejected credential needs a new one, an unreadable
        answer is the server's problem, and a disabled integration was their own
        earlier decision.
        """
        integration = await self.get(integration_id)
        if not integration.enabled:
            return integration

        await self._save(integration, IntegrationStatus.CONNECTING)
        try:
            connection = await self._open(integration)
        except IntegrationAuthenticationError as error:
            return await self._failed(
                integration, IntegrationStatus.AUTHENTICATION_REQUIRED, error
            )
        except IntegrationError as error:
            return await self._failed(
                integration, IntegrationStatus.CONNECTION_FAILED, error
            )

        self._register(connection)
        ready = connection.integration
        await self._repository.save(ready)
        log.info(
            "integration.connected",
            integration=ready.name,
            tools=len(ready.discovered),
        )
        await self._changed()
        return ready

    async def _open(self, integration: Integration) -> Connection:
        """Build the connection and discover, with the record left to the caller."""
        return await self._connector(integration, self._environment(integration))

    async def _reopen(self, integration: Integration) -> Connection:
        """The start-up path: the tools it already told us about."""
        return await self._restorer(integration, self._environment(integration))

    def _environment(self, integration: Integration) -> dict[str, str]:
        """The credentials this server needs, resolved at the moment of use.

        Named on the record, resolved here, handed to the child process and
        never written anywhere. A name with nothing behind it is left out rather
        than passed as an empty string: a server told its token is "" fails in a
        way nobody can read, and its absence is at least the truth.
        """
        found = {}
        for name in integration.secret_names:
            secret = self._secrets.maybe(name) if self._secrets is not None else None
            if secret is None and name in self._provided:
                found[name] = self._provided[name]
                continue
            if secret is None:
                log.info(
                    "integration.secret_missing",
                    integration=integration.name,
                    secret=name,
                )
                continue
            found[name] = secret.reveal()
        return found

    # --- Turning off and removing ---------------------------------------------

    async def disable(self, integration_id: UUID) -> Integration:
        """Stop offering it, keep everything about it.

        Deleting a user's setup in order to pause it would make disabling the
        more destructive of the two operations, which is the opposite of what
        the word means.
        """
        integration = await self.get(integration_id)
        await self._disconnect(integration)
        disabled = integration.set_enabled(False).failed(IntegrationStatus.DISABLED)
        await self._repository.save(disabled)
        log.info("integration.disabled", integration=integration.name)
        await self._changed()
        return disabled

    async def enable(self, integration_id: UUID) -> Integration:
        """Switch it back on, and reconnect if it can be reconnected.

        No setup is asked for again: the configuration and the credentials are
        where they were, which is the whole point of disable being reversible.
        """
        integration = await self.get(integration_id)
        enabled = integration.set_enabled(True).failed(IntegrationStatus.DISCONNECTED)
        await self._repository.save(enabled)
        return await self.connect(enabled.id)

    async def remove(self, integration_id: UUID) -> bool:
        """Disconnect, forget the configuration, keep the history.

        What it did stays in `tool_calls` and `audit_log`, which have no foreign
        keys for exactly this reason: removing a service must not erase the
        record of what was done with it (ADR 0010).
        """
        integration = await self.get(integration_id)
        await self._disconnect(integration)
        removed = await self._repository.delete(integration_id)
        log.info("integration.removed", integration=integration.name)
        await self._changed()
        return removed

    async def _disconnect(self, integration: Integration) -> None:
        """Take its tools off the table and close the connection if there is one.

        The names come from the stored record rather than from the live
        connection, because the case that matters is the one where there is no
        live connection - a server that died, or a process that restarted since
        it was registered.
        """
        for name in integration.tool_names:
            self._registry.unregister(name)
        connection = self._live.pop(integration.id, None)
        if connection is None:
            return
        try:
            result = connection.close()
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        except Exception as error:  # closing must not fail the operation
            log.warning(
                "integration.close_failed", integration=integration.name, error=str(error)
            )

    # --- Asking a server to sign in ---------------------------------------------

    async def try_tool(
        self, integration_id: UUID, tool_name: str, arguments: dict[str, object]
    ) -> bool:
        """Call one of its tools on a person's behalf, and say only whether it worked.

        What a sign-in button needs: a server that signs in through the browser
        opens its own page when a call arrives unauthorised, and answers with
        data once it is not. The reply is not read - success is the whole
        answer, so no wording of any server is parsed. Only a tool the plugin's
        declaration names is ever asked for (see the facade), and it is a read.
        """
        integration = await self.get(integration_id)
        if integration.id not in self._live:
            integration = await self.connect(integration_id)
        connection = self._live.get(integration.id)
        if connection is None:
            return False
        wanted = integration.qualified(tool_name)
        tool = next((one for one in connection.tools if one.spec.name == wanted), None)
        if tool is None:
            return False
        result = await tool.execute(dict(arguments))
        log.info("integration.tool_tried", integration=integration.name, succeeded=result.success)
        return result.success

    # --- Who may use it ---------------------------------------------------------

    async def grant(self, integration_id: UUID, employees: frozenset[str]) -> Integration:
        """Say which employees may use it from the window.

        Replaces the machine's list and leaves every employee file alone: a
        name written in a declaration stays granted whatever is chosen here,
        because the file is the other half of the rule and this is not the
        place to edit it.
        """
        integration = await self.get(integration_id)
        chosen = integration.granted_to_only(employees)
        await self._repository.save(chosen)
        log.info(
            "integration.granted",
            integration=integration.name,
            employees=sorted(chosen.granted_to),
        )
        await self._changed()
        return chosen

    # --- Classifying what it offers -------------------------------------------

    async def classify(self, integration_id: UUID, effects: dict[str, Effect]) -> Integration:
        """Say what its tools do to the world, and re-register at the new risk.

        Re-registering is the point: a spec's effect decides its risk, the risk
        is read off the spec when a call is gated, and a tool already in the
        registry carries the effect it was built with. Storing the new
        classification without rebuilding would leave the old one in force,
        which is the silent half of this operation.
        """
        integration = await self.get(integration_id)
        classified = replace(
            integration, effects={**integration.effects, **effects}
        )
        await self._repository.save(classified)
        if classified.id in self._live:
            await self._disconnect(classified)
            await self.connect(classified.id)
        else:
            await self._changed()
        return await self.get(integration_id)

    # --- Start-up -------------------------------------------------------------

    async def restore(self) -> int:
        """Make this process's registry match what is stored.

        Uses the cached tools rather than reconnecting: what a server offered is
        on the record, and starting every configured subprocess at boot would
        make the runtime's start time the sum of theirs. The connection happens
        on the first call that needs it.
        """
        restored = 0
        for integration in await self._repository.list():
            if not integration.is_usable or not integration.discovered:
                continue
            try:
                connection = await self._reopen(integration)
            except IntegrationError as error:
                log.info(
                    "integration.not_restored",
                    integration=integration.name,
                    error=str(error),
                )
                continue
            self._register(connection)
            restored += 1
        await self._changed()
        return restored

    async def aclose(self) -> None:
        """Close every server this process opened, and none that it did not."""
        for connection in list(self._live.values()):
            await self._disconnect(connection.integration)

    # --- Plumbing -------------------------------------------------------------

    def _register(self, connection: Connection) -> None:
        for tool in connection.tools:
            self._registry.register(tool)
        self._live[connection.integration.id] = connection

    async def _save(self, integration: Integration, status: IntegrationStatus) -> None:
        await self._repository.save(integration.failed(status))

    async def _failed(
        self, integration: Integration, status: IntegrationStatus, error: Exception
    ) -> Integration:
        failed = integration.failed(status)
        await self._repository.save(failed)
        log.info(
            "integration.connection_failed",
            integration=integration.name,
            status=status.value,
            error=str(error),
        )
        await self._changed()
        return failed

    async def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(await self._repository.list())
        except Exception as error:  # a watcher must not fail the operation
            log.warning("integration.change_not_announced", error=str(error))


__all__ = [
    "Connection",
    "Connector",
    "DiscoveredTool",
    "DuplicateIntegrationError",
    "IntegrationNotFoundError",
    "IntegrationService",
]
