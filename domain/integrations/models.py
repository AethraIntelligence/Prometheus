"""An external service the user connected, and what the platform makes of it.

Every capability before this one was written here: a tool is a file, its author
declares an `Effect`, and ADR 0010 turns that declaration into a risk level
nobody can lower. An integration breaks that assumption in one place - the tools
arrive at runtime, from a process this platform did not write, describing
themselves - and the two values on this record are what put it back.

`effects` is the local answer to "what does this tool do to the world". It is
written on this machine and it is the only thing the policy layer reads. A
server's own description of its tools may be *proposed* to a person and may
never become the stored value on its own: the party whose actions are being
classified is not the party that classifies them (ADR 0015, §24).

`granted_capabilities` is the local answer to "what work can reach this". It is
what an employee holding this integration adds to its own capabilities, so the
manager can route by it - an ability nobody declared is work that never arrives.

Both are data on a record rather than something derived from the server, and
that is the whole of the trust boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from domain.capabilities.models import Capability
from domain.policies.risk import Effect
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

IntegrationId = UUID

#: What a discovered tool does to the world until somebody says otherwise.
#: EXECUTE is HIGH, and HIGH asks. A tool nobody has classified could be
#: anything, and the two ways of being wrong are not symmetrical: one costs a
#: question, the other costs an action the user never approved.
UNCLASSIFIED_EFFECT = Effect.EXECUTE


class IntegrationKind(StrEnum):
    """How the platform reaches this service.

    MCP is one mechanism and not the foundation: the kind exists so that a
    native API integration is a new member here rather than a second system.
    """

    MCP = "MCP"
    NATIVE = "NATIVE"


class IntegrationStatus(StrEnum):
    """Where an integration is in its life, including the ways it goes wrong.

    Failure is a status rather than an exception because the record outlives the
    process that failed to connect: a person opening the window tomorrow needs
    to be told what happened, and "there is no row" cannot tell them.
    """

    DISCONNECTED = "DISCONNECTED"
    CONFIGURING = "CONFIGURING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCOVERING = "DISCOVERING"
    READY = "READY"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    """One tool an integration says it offers, as it arrived.

    Kept as the server sent it - name, sentence, schema - and turned into a
    `ToolSpec` elsewhere, so the untrusted thing and the thing the platform acts
    on are never the same object.
    """

    name: str
    description: str = ""
    json_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Integration:
    """One connected service."""

    id: IntegrationId
    name: str
    kind: IntegrationKind = IntegrationKind.MCP
    status: IntegrationStatus = IntegrationStatus.DISCONNECTED
    #: How to reach it. For MCP over stdio this is the command and its
    #: arguments; the shape is the transport's business, not the domain's.
    configuration: dict[str, Any] = field(default_factory=dict)
    #: Tool name (as the server calls it) to what it does to the world.
    effects: dict[str, Effect] = field(default_factory=dict)
    #: What an employee granted this integration can thereby be asked to do.
    granted_capabilities: frozenset[Capability] = field(default_factory=frozenset)
    #: Names of secrets this integration needs, resolved at the moment of a call
    #: and never stored here. The value never appears on this record.
    secret_names: tuple[str, ...] = ()
    #: What it offered when it was last asked, kept so that listing the tools on
    #: this machine, checking an employee declaration and planning a task do not
    #: have to start a subprocess. Written at discovery and read everywhere
    #: else; the server is only reached when a call is actually made.
    discovered: tuple[DiscoveredTool, ...] = ()
    #: Switched off without being forgotten: the configuration stays, the
    #: capabilities stop being offered. Deleting a user's setup to pause it
    #: would make "disable" the more destructive of the two operations.
    enabled: bool = True
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    #: Employees granted this integration on this machine, by name. The other
    #: way to grant one is `integrations:` in the employee's own file; this is
    #: the one a person uses from the window, and like that one it can only
    #: add (see `domain/integrations/grants.py`). Names rather than ids for the
    #: reason a declaration uses names: the directory is the identity.
    granted_to: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def create(cls, name: str, **extra: Any) -> Integration:
        if not name.strip():
            raise ValueError("an integration needs a name")
        return cls(id=uuid4(), name=name.strip(), **extra)

    def effect_for(self, tool_name: str) -> Effect:
        """What one of its tools does to the world, as declared *here*."""
        return self.effects.get(tool_name, UNCLASSIFIED_EFFECT)

    def qualified(self, tool_name: str) -> str:
        """The name this tool has inside the platform.

        Namespaced by the integration so two servers offering `search` are two
        tools, and so an employee's declaration names one of them and not both.
        """
        return f"{self.name}.{tool_name}"

    def rediscovered(self, tools: tuple[DiscoveredTool, ...]) -> Integration:
        """What it offers now, and READY because it answered to say so."""
        return replace(self, discovered=tools, status=IntegrationStatus.READY)

    def failed(self, status: IntegrationStatus) -> Integration:
        """A way it went wrong, kept on the record rather than raised and lost.

        The previously discovered tools stay: a server that is unreachable this
        minute has not stopped offering what it offered, and forgetting the list
        would mean an employee declaration cannot be checked while a machine is
        offline.
        """
        return replace(self, status=status)

    def granted_to_only(self, employees: frozenset[str]) -> Integration:
        """Who may use it from now on, as a person chose from the window."""
        chosen = frozenset(name.strip() for name in employees if name.strip())
        return replace(self, granted_to=chosen)

    def set_enabled(self, enabled: bool) -> Integration:
        """Switched off without being forgotten, and back on without setup."""
        return replace(self, enabled=enabled)

    @property
    def tool_names(self) -> frozenset[str]:
        """Every name this integration contributes to the registry."""
        return frozenset(self.qualified(tool.name) for tool in self.discovered)

    @property
    def is_usable(self) -> bool:
        """Whether its capabilities should be offered at all right now."""
        return self.enabled and self.status is not IntegrationStatus.DISABLED
