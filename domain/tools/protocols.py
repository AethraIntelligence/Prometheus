from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from domain.approvals.gate import RiskAssessment
from domain.policies.models import Actor
from domain.tools.models import ToolResult, ToolSpec


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def execute(self, input_data: dict[str, Any]) -> ToolResult: ...


@runtime_checkable
class RiskAssessor(Protocol):
    """An optional second half of `Tool`, for tools whose risk is per call.

    Writing a file that does not exist and overwriting one that does are the
    same tool and very different actions. A tool that can tell them apart says
    so here; one that cannot simply does not implement this, and its declared
    `ToolSpec.risk_level` stands.
    """

    def assess(self, input_data: dict[str, Any]) -> RiskAssessment | None: ...


@runtime_checkable
class EffectPreviewer(Protocol):
    """Optional safe preview produced before a tool changes the world."""

    def preview(self, input_data: dict[str, Any]) -> dict[str, Any]: ...


class ToolRegistry(Protocol):
    """Permissions are enforced here, not in the caller.

    An actor only ever sees the specs it is allowed to use, so a model cannot ask
    for a tool it has no right to.
    """

    def register(self, tool: Tool) -> None: ...

    def unregister(self, name: str) -> bool: ...

    def get(self, name: str, actor: Actor) -> Tool: ...

    def list_specs(self, actor: Actor) -> list[ToolSpec]: ...
