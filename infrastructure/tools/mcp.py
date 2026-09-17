"""A tool that happens to live in another process.

This is the whole of MCP's contact with the rest of the platform. It subclasses
`BaseTool` like every other tool, so it gets the same argument validation, the
same coercion, the same `ignored_arguments` reporting and the same rule that a
tool may not take the task down with it. Above it, nothing knows where it came
from: the registry stores it, the gate gates it, the executor calls it, and none
of them has a branch for it. A second tool system - an MCP registry, an MCP
executor - would be a second answer to a question this repository answered in
Phase 4.

Three things are decided here rather than by the server, and each is a rule from
ADR 0015 made structural:

**The effect is local.** It comes off the `Integration` record, not off the
tool's description of itself, and an unclassified tool is EXECUTE - which is
HIGH, which asks. The server does not get a vote on whether its own actions
need approval.

**The name is namespaced.** `gmail.send_message`, so two servers offering
`search` are two tools, an employee's declaration names one of them, and a
trace read months later says which service was reached.

**The result is framed as untrusted.** Whatever comes back is external content
on its way into a model's context, so it is wrapped before it leaves this
method. Doing it here rather than in the executor is what keeps the executor
free of the branch, and it means a tool cannot forget.
"""

from __future__ import annotations

from typing import Any

import structlog

from domain.capabilities.models import Capability
from domain.errors import IntegrationError
from domain.integrations.models import DiscoveredTool, Integration
from domain.integrations.specs import spec_for
from domain.integrations.untrusted import NOTE, frame
from domain.tools.models import ToolResult
from infrastructure.mcp.client import MCPClient
from infrastructure.tools.base import BaseTool

log = structlog.get_logger(__name__)


class MCPTool(BaseTool):
    """One tool on one connected server."""

    def __init__(
        self,
        integration: Integration,
        discovered: DiscoveredTool,
        client: MCPClient,
        *,
        connect: bool = True,
    ) -> None:
        super().__init__(spec_for(integration, discovered))
        self._integration = integration
        self._remote_name = discovered.name
        self._client = client
        # Connecting on first use rather than at registration: listing the
        # tools on this machine must not start every configured server, and an
        # employee declaration has to be checkable where none is running.
        self._connect = connect

    @property
    def integration_id(self) -> str:
        """Provenance, for the audit line and the interface. Not for the model."""
        return str(self._integration.id)

    async def run(self, **arguments: Any) -> ToolResult:
        if self._connect:
            await self._client.connect()
        try:
            content = await self._client.call_tool(self._remote_name, arguments)
        except IntegrationError as error:
            # Typed, so `application.orchestrator` can tell "try again" from
            # "this will keep happening" without reading the message.
            log.info(
                "integration.call_failed",
                integration=self._integration.name,
                tool=self._remote_name,
                transient=error.transient,
                error=str(error),
            )
            return ToolResult.failure(f"{type(error).__name__}: {error}")

        return ToolResult.ok(
            source=f"{self._integration.name} (external service)",
            note=NOTE,
            content=frame(content, origin=self._integration.name, kind="integration"),
        )


def tools_for(
    integration: Integration,
    discovered: tuple[DiscoveredTool, ...],
    client: MCPClient,
) -> list[MCPTool]:
    """Everything one integration contributes to the registry."""
    return [MCPTool(integration, tool, client) for tool in discovered]


#: Capabilities an integration may grant. Re-exported so a caller building one
#: does not have to know that the vocabulary is closed and where it lives.
__all__ = ["Capability", "MCPTool", "spec_for", "tools_for"]
